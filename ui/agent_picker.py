"""Terminal-only welcome screen and provider selection, before engine startup."""
from __future__ import annotations

from dataclasses import dataclass
import math
import os
from pathlib import Path
import shutil
import time

from prompt_toolkit.application import Application
from prompt_toolkit.data_structures import Point
from prompt_toolkit.filters import Condition
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Float, FloatContainer, HSplit, Layout, VSplit, Window, WindowAlign
from prompt_toolkit.layout.controls import FormattedTextControl, UIContent, UIControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.mouse_events import MouseEventType
from prompt_toolkit.output import ColorDepth
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import Box

from ..core.config import TetherConfig, apply_provider_selection, default_provider_model
from .workspace import clean_label, provider_identity


@dataclass(frozen=True)
class AgentChoice:
    provider: str
    icon: str
    name: str
    color: str
    model: str
    status: str
    setup: str = ""


def agent_choices(config: TetherConfig) -> list[AgentChoice]:
    """Use the local catalog and credential presence; never contact a provider."""
    choices = []
    providers = ["anthropic", "codex", "openai", "deepseek", "kimi", "glm", "local"]
    if config.provider not in providers:
        providers.append(config.provider)
    for provider in providers:
        icon, name, color = provider_identity(provider)
        model = config.api_model if provider == config.provider else default_provider_model(provider)
        setup = ""
        if provider == "local":
            model = Path(config.model_path).name if config.model_path else "Choose a GGUF model"
            status = "On your machine"
        elif provider == "codex":
            status = "Codex CLI login"
            if not shutil.which("codex"):
                status = "Setup needed"
                setup = "Install the Codex CLI and run codex login --device-auth, then restart Tether."
        elif config.has_api_key(provider):
            status = "API key ready"
        else:
            status = "API key needed"
            env_names = config.api_key_env_names(provider)
            setup = (f"Set {env_names[0]} in your shell, then restart Tether."
                     if env_names else f"Configure {name} with tether remote and tether key set first.")
        choices.append(AgentChoice(provider, icon, name, color, clean_label(model), status, setup))
    return choices


class BouncingLight(UIControl):
    """A small glowing orb drawn entirely in terminal cells."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.started = clock()

    @staticmethod
    def _bounce(elapsed, speed, extent):
        if extent <= 0:
            return 0
        phase = (extent / 2 + elapsed * speed) % (2 * extent)
        return extent - abs(phase - extent)

    def position(self, width, height):
        elapsed = max(0, self.clock() - self.started)
        margin_x = min(5, max(0, (width - 1) / 2))
        margin_y = min(1.5, max(0, (height - 1) / 2))
        return (margin_x + self._bounce(elapsed, 9, width - 1 - 2 * margin_x),
                margin_y + self._bounce(elapsed, 1.8, height - 1 - 2 * margin_y))

    def create_content(self, width, height):
        cx, cy = self.position(width, height)
        core_x, core_y = round(cx), round(cy)
        lines = []
        for y in range(height):
            line = []
            for x in range(width):
                radius = math.hypot(x - cx, (y - cy) * 2.1)
                strength = max(0.0, 1 - radius / 5.2) ** 2
                background = tuple(round(dark + (light - dark) * strength)
                                   for dark, light in zip((16, 17, 22), (45, 127, 164)))
                style = "bg:#%02x%02x%02x" % background
                if (x, y) == (core_x, core_y):
                    char, style = "●", style + " #edfcff bold"
                elif radius < 2:
                    char, style = "·", style + " #88ddf5"
                else:
                    char = " "
                line.append((style, char))
            lines.append(line)
        return UIContent(get_line=lambda row: lines[row], line_count=height, show_cursor=False)


class AgentPicker:
    def __init__(self, config: TetherConfig, *, input=None, output=None, clock=time.monotonic):
        self.config = config
        self.choices = agent_choices(config)
        self.selected = next((i for i, choice in enumerate(self.choices)
                              if choice.provider == config.provider), 0)
        self.message = ""
        self.dialog = None
        self.orb = BouncingLight(clock)
        bindings = KeyBindings()
        no_dialog = Condition(lambda: self.dialog is None)
        for key, delta in (("up", -1), ("k", -1), ("down", 1), ("j", 1), ("tab", 1), ("s-tab", -1)):
            bindings.add(key, filter=no_dialog)(lambda event, d=delta: self.move(d))

        @bindings.add("home", filter=no_dialog)
        def first(event):
            self.selected = 0
            self.message = ""

        @bindings.add("end", filter=no_dialog)
        def last(event):
            self.selected = len(self.choices)
            self.message = ""

        @bindings.add("enter", filter=no_dialog)
        def choose(event):
            self.choose()

        @bindings.add("escape", filter=no_dialog)
        @bindings.add("c-c", filter=no_dialog)
        @bindings.add("c-d", filter=no_dialog)
        def cancel(event):
            event.app.exit(result=None)

        @bindings.add("f2", filter=no_dialog)
        def settings(event):
            self.open_settings()

        @bindings.add("escape", filter=~no_dialog)
        @bindings.add("c-c", filter=~no_dialog)
        def dismiss(event):
            self.close_dialog()

        self.menu = FormattedTextControl(self._rows, focusable=True, show_cursor=False,
                                        get_cursor_position=lambda: Point(0, self.selected * self._row_height()))
        panel = HSplit([
            Window(FormattedTextControl("◇ TETHER"), height=1, align=WindowAlign.CENTER, style="class:brand"),
            Window(self.orb, height=lambda: 7 if self._roomy() else 3),
            Window(FormattedTextControl("Select your agent"), height=1, align=WindowAlign.CENTER, style="class:title"),
            Window(FormattedTextControl("Who are we building with today?"), height=1,
                   align=WindowAlign.CENTER, style="class:muted"),
            Window(height=1),
            VSplit([Window(width=4), Window(self.menu, height=lambda: (len(self.choices) + 1) * self._row_height()),
                    Window(width=4)]),
            Window(FormattedTextControl(self._detail), height=2, wrap_lines=True,
                   align=WindowAlign.CENTER, style="class:muted"),
            Window(FormattedTextControl("↑ ↓ choose · Enter start · F2 settings · Esc exit"), height=1,
                   align=WindowAlign.CENTER, style="class:muted"),
        ], width=Dimension(min=24, preferred=54, max=54))
        self.root = FloatContainer(
            Box(panel, padding_top=Dimension(min=0), padding_bottom=Dimension(min=0),
                style="class:welcome"), floats=[])
        self.app = Application(
            layout=Layout(self.root, focused_element=self.menu),
            key_bindings=bindings, input=input, output=output, full_screen=True, mouse_support=True,
            refresh_interval=0.05, min_redraw_interval=0.04,
            color_depth=ColorDepth.DEPTH_1_BIT if "NO_COLOR" in os.environ else None,
            style=Style.from_dict({
                "welcome": "bg:#101116 #dedee5",
                "brand": "#b69af8 bold",
                "title": "#eeeeF4 bold",
                "muted": "#9193a5",
                "choice": "bg:#252c40 noreverse bold",
                "claude": "#e3a17e",
                "openai": "#8ad4bb",
                "blue": "#86b9e8",
                "purple": "#b69af8",
                "warning": "#e4bd7a",
                "dialog": "bg:#171820 #dedee5",
                "dialog.body": "bg:#171820 #dedee5",
                "dialog.body text-area": "bg:#101116 #dedee5",
                "dialog.body text-area last-line": "nounderline",
                "dialog frame.label": "#b69af8 bold",
                "button.focused": "bg:#2c293d #dedee5 noreverse",
            }),
        )
        self.app.ttimeoutlen = 0.1

    def _roomy(self):
        return not hasattr(self, "app") or self.app.output.get_size().rows >= 36

    def _row_height(self):
        return 2 if self._roomy() else 1

    def move(self, delta):
        self.selected = (self.selected + delta) % (len(self.choices) + 1)
        self.message = ""

    def _rows(self):
        fragments = []
        for i, choice in enumerate(self.choices):
            def click(event, index=i):
                if event.event_type == MouseEventType.MOUSE_UP:
                    self.selected = index
                    self.choose()

            selected = " class:choice" if i == self.selected else ""
            marker = "›" if selected else " "
            fragments.extend([
                (f"class:{choice.color}{selected}", f" {marker} {choice.icon}  {choice.name:<12}", click),
                (f"class:muted{selected}", f"{choice.status:<28}\n", click),
            ])
            if self._row_height() == 2:
                fragments.append(("", "\n"))
        def settings_click(event):
            if event.event_type == MouseEventType.MOUSE_UP:
                self.open_settings()

        selected = " class:choice" if self.selected == len(self.choices) else ""
        fragments.append((f"class:brand{selected}",
                          f" {'›' if selected else ' '} ⚙  Settings     Shell, themes & fonts\n", settings_click))
        return fragments

    def _detail(self):
        if self.message:
            return [("class:warning", self.message)]
        if self.selected == len(self.choices):
            return "Configure shell appearance before starting a chat."
        return self.choices[self.selected].model

    def choose(self):
        if self.selected == len(self.choices):
            self.open_settings()
            return
        choice = self.choices[self.selected]
        if choice.setup:
            self.message = choice.setup
            return
        self.app.exit(result=choice.provider)

    def open_settings(self):
        if self.dialog is not None:
            return
        from .shell_dialog import ShellDialog
        from .shell_theme import save_settings

        def save(shell, theme, font, theme_file):
            try:
                save_settings(self.config, shell, theme, font, theme_file)
            except (OSError, ValueError) as exc:
                self.dialog.message = clean_label(exc)
                self.app.invalidate()
                return
            self.close_dialog()
            self.message = "Shell settings saved. Choose an agent when you're ready."
            self.app.invalidate()

        self.dialog = ShellDialog(self.config, save, self.close_dialog)
        self.root.floats.append(Float(content=self.dialog.dialog))
        self.app.layout.update_parents_relations()
        self.app.layout.focus(self.dialog.search)
        self.app.invalidate()

    def close_dialog(self):
        self.root.floats.clear()
        self.dialog = None
        self.app.layout.focus(self.menu)
        self.app.invalidate()

    def run(self):
        try:
            return self.app.run()
        except (EOFError, KeyboardInterrupt):
            return None


def select_agent(config: TetherConfig) -> bool:
    provider = AgentPicker(config).run()
    if provider is None:
        return False
    if provider != config.provider:
        apply_provider_selection(config, provider)
    return True
