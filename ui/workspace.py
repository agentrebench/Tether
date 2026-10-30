"""Persistent terminal workspace around the existing REPL and turn controller.

The UI owns the terminal on the main thread; the REPL consumes an input queue
on a worker. All engine output is captured, so streaming never paints over the
sidebar, composer, or approval prompt.
"""
from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import os
import queue
import re
import sys
import threading
from collections import deque
from pathlib import Path

from prompt_toolkit.application import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.data_structures import Point
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.key_binding import ConditionalKeyBindings, KeyBindings, merge_key_bindings
from prompt_toolkit.layout import (
    ConditionalContainer, DynamicContainer, Float, FloatContainer, HSplit, Layout, VSplit, Window,
)
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.layout.processors import BeforeInput
from prompt_toolkit.mouse_events import MouseEventType
from prompt_toolkit.output import ColorDepth
from prompt_toolkit.styles import Style
from prompt_toolkit.utils import get_cwidth

from .file_tree import FileTree, file_preview
from .panes import Pane, PaneLayout, Split


def provider_identity(provider: str, model: str = "") -> tuple[str, str, str]:
    """Font-independent terminal marks, with readable names as a fallback."""
    identities = {
        "anthropic": ("✳", "Claude", "claude"),
        "openai": ("◉", "GPT", "openai"),
        "codex": ("◈", "Codex", "openai"),
        "deepseek": ("◆", "DeepSeek", "blue"),
        "kimi": ("☾", "Kimi", "purple"),
        "glm": ("◇", "GLM", "blue"),
        "local": ("▣", "Local", "purple"),
    }
    return identities.get(provider, ("◇", provider.title() or "Custom", "purple"))


def clean_label(value: object) -> str:
    return " ".join(re.sub(r"[\x00-\x1f\x7f-\x9f]", " ", str(value)).split())


class Transcript(io.TextIOBase):
    """Bounded, thread-safe stdout sink; carriage returns replace progress."""

    def __init__(self, invalidate=lambda: None, max_lines: int = 4000):
        self._lines = deque(maxlen=max_lines)
        self._tail = ""
        self._lock = threading.Lock()
        self._invalidate = invalidate
        self.revision = 0

    def isatty(self):
        return True

    def write(self, value: str) -> int:
        # Keep SGR colors, discard cursor controls (including erase-line).
        value_clean = re.sub(
            r"\x1b\[[0-?]*[ -/]*[@-~]",
            lambda match: match.group() if match.group().endswith("m") else "", value,
        )
        with self._lock:
            for part in re.split(r"([\r\n])", value_clean):
                if part == "\r":
                    self._tail = ""
                elif part == "\n":
                    self._lines.append(self._tail)
                    self._tail = ""
                else:
                    self._tail = (self._tail + part)[-24000:]
            self.revision += 1
        self._invalidate()
        return len(value)

    def text(self) -> str:
        with self._lock:
            return "\n".join([*self._lines, self._tail])

    def flush(self):
        pass


class StatusOutput(io.TextIOBase):
    """Keep the engine's animated stderr spinner out of the conversation."""

    def __init__(self, transcript: Transcript):
        self.transcript = transcript
        self.status = ""

    def write(self, value: str) -> int:
        if "\r" in value:
            self.status = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", value.rsplit("\r", 1)[-1]).strip()
            self.transcript._invalidate()
        else:
            self.transcript.write(value)
        return len(value)

    def flush(self):
        pass


class TerminalWorkspace:
    SIDEBAR_WIDTH = 29

    def __init__(self, repl, *, input=None, output=None):
        from .repl import TetherCompleter, _build_key_bindings

        self.repl = repl
        self.inbox: queue.Queue = queue.Queue()
        self.closed = threading.Event()
        self.agents: dict[str, dict] = {}
        self._agent_lock = threading.Lock()
        self.panes = PaneLayout()
        self.host_pane = self.panes.active
        self._pane_widgets = {}
        self._closing_shells = set()
        self.file_tree = FileTree(Path.cwd())
        self.files_visible = False
        self.dialog = None
        self._modal_float = None
        self._directory_pending = False
        self.notice = ""
        self.selected = "host"
        self.sidebar_visible = True
        self.mouse_enabled = False  # native selection and copy/paste by default; F10 enables clicks
        self.scroll_line: int | None = None
        self._cached_revision = -1
        self._cached_transcript = ""
        self._cached_lines = 1
        self.transcript = Transcript(self.invalidate)
        self.transcript.terminal_width = self._content_width
        self.stderr = StatusOutput(self.transcript)

        self.buffer = Buffer(
            multiline=True, history=repl._history,
            completer=TetherCompleter(skill_store=repl.skill_store, config=repl.config),
            complete_while_typing=True, accept_handler=self._submit,
        )
        self.composer = composer = Window(
            BufferControl(self.buffer, input_processors=[BeforeInput(" › ")]),
            height=self._composer_height, wrap_lines=True,
            style="class:composer",
        )
        self.content = FormattedTextControl(
            self._content_text, get_cursor_position=self._cursor, focusable=True,
        )
        self.content_window = Window(self.content, wrap_lines=True)
        self.tree_control = FormattedTextControl(
            self._tree_text, focusable=True, key_bindings=self._tree_bindings(),
            get_cursor_position=lambda: Point(0, self.file_tree.selected),
        )
        self.tree_window = Window(self.tree_control, wrap_lines=False)
        sidebar = ConditionalContainer(
            HSplit([
                Window(FormattedTextControl(self._sidebar), wrap_lines=False,
                       height=lambda: Dimension(min=9, preferred=18, max=20) if self.files_visible else Dimension()),
                ConditionalContainer(HSplit([
                    Window(FormattedTextControl(self._files_header), height=2),
                    self.tree_window,
                ]), filter=Condition(lambda: self.files_visible)),
                Window(FormattedTextControl("  alt+↑/↓  select agent\n  ctrl+b   toggle sidebar"),
                       height=3, style="class:muted"),
            ], width=self.SIDEBAR_WIDTH, style="class:sidebar"),
            filter=Condition(self._show_sidebar),
        )
        divider = ConditionalContainer(
            Window(width=1, char="│", style="class:border"),
            filter=Condition(self._show_sidebar),
        )
        self._host_container = HSplit([
            Window(FormattedTextControl(self._header), height=2, style="class:header"),
            self.content_window,
            Window(FormattedTextControl(self._activity), height=1, style="class:muted"),
            Window(height=1, char="─", style="class:border"),
            composer,
            Window(FormattedTextControl(self._footer), height=1, style="class:muted"),
        ], width=Dimension(min=1, preferred=1), height=Dimension(min=1, preferred=1))
        self._pane_widgets[self.host_pane.id] = self._host_container
        self._pane_tree = self._host_container
        main = HSplit([
            Window(FormattedTextControl(self._pane_toolbar), height=2, style="class:toolbar"),
            DynamicContainer(self._visible_panes),
        ])
        self.root = root = FloatContainer(
            # A default Style rule alone does not paint untouched blank cells:
            # the renderer treats them as identical to its empty screen. Give
            # the entire layout an explicit class so every pane is painted.
            content=VSplit([sidebar, divider, main], style="class:workspace"),
            floats=[Float(xcursor=True, ycursor=True,
                          content=CompletionsMenu(max_height=10, scroll_offset=1))],
        )
        kb = KeyBindings()
        in_composer = Condition(lambda: self.dialog is None and self.app.layout.has_focus(self.buffer))
        no_dialog = Condition(lambda: self.dialog is None)

        @kb.add("c-c", filter=in_composer)
        def interrupt(event):
            if repl._controller and repl._controller.is_busy():
                repl._controller.interrupt()
                repl._broker.cancel()
                self.transcript.write("\nInterrupting…\n")
            elif self.buffer.text:
                self.buffer.reset()
            else:
                self.inbox.put(KeyboardInterrupt())

        @kb.add("c-d", filter=in_composer)
        def quit_app(event):
            if not self.buffer.text:
                self.closed.set()
                repl._broker.cancel()
                if repl._controller:
                    repl._controller.interrupt()
                self.inbox.put(EOFError())

        @kb.add("c-b", filter=in_composer)
        def toggle_sidebar(event):
            self.sidebar_visible = not self.sidebar_visible

        @kb.add("escape", "up", filter=in_composer)
        def previous_agent(event):
            self._select_relative(-1)

        @kb.add("escape", "down", filter=in_composer)
        def next_agent(event):
            self._select_relative(1)

        @kb.add("escape", filter=in_composer)
        def host(event):
            self.select("host")

        @kb.add("pageup", filter=in_composer)
        def page_up(event):
            self._scroll(-max(1, self.app.output.get_size().rows - 12))

        @kb.add("pagedown", filter=in_composer)
        def page_down(event):
            self._scroll(max(1, self.app.output.get_size().rows - 12))

        @kb.add("c-end", filter=in_composer)
        def follow_output(event):
            self.scroll_line = None

        for direction in ("left", "right", "up", "down"):
            kb.add("c-w", direction, filter=no_dialog)(lambda event, d=direction: self.split_pane(d))

        @kb.add("f2", filter=no_dialog)
        def settings(event):
            self.open_settings()

        @kb.add("f3", filter=no_dialog)
        def files(event):
            self.toggle_files()

        @kb.add("f10", filter=no_dialog)
        @kb.add("escape", "m", filter=no_dialog)
        def toggle_mouse(event):
            self.toggle_mouse()

        @kb.add("f6", filter=no_dialog)
        def next_pane(event):
            self.panes.cycle()
            self.focus_pane(self.panes.active)

        @kb.add("c-w", "p", filter=no_dialog)
        def previous_pane(event):
            self.panes.cycle(-1)
            self.focus_pane(self.panes.active)

        @kb.add("c-w", "x", filter=no_dialog)
        def close_pane(event):
            self.close_pane()

        @kb.add("escape", filter=~no_dialog)
        @kb.add("c-c", filter=~no_dialog)
        def dismiss_dialog(event):
            self.close_dialog()

        self.app = Application(
            layout=Layout(root, focused_element=composer),
            key_bindings=merge_key_bindings([ConditionalKeyBindings(_build_key_bindings(), in_composer), kb]),
            full_screen=True, mouse_support=Condition(lambda: self.mouse_enabled),
            min_redraw_interval=0.05, refresh_interval=0.2,
            input=input, output=output,
            color_depth=ColorDepth.DEPTH_1_BIT if "NO_COLOR" in os.environ else None,
            style=Style.from_dict({
                "": "bg:#101116 #dedee5",
                "workspace": "bg:#101116 #dedee5",
                "sidebar": "bg:#171820 #dedee5",
                "header": "bold #dedee5",
                "toolbar": "bg:#171820 #b69af8",
                "pane-active": "bg:#2c293d bold noreverse",
                "folder-list": "bg:#171820 #dedee5",
                "folder-selected": "bg:#2c293d #dedee5 noreverse",
                "dialog": "bg:#171820 #dedee5",
                "dialog.body": "bg:#171820 #dedee5",
                "dialog.body text-area": "bg:#101116 #dedee5",
                "dialog.body text-area last-line": "nounderline",
                "dialog frame.label": "#b69af8 bold",
                "button.focused": "bg:#2c293d #dedee5 noreverse",
                "brand": "bold #b69af8",
                "muted": "#9193a5",
                "border": "#393b4b",
                "composer": "bg:#171820 #eeeeF4",
                # Avoid prompt_toolkit's built-in `selected` (reverse video).
                "agent-selected": "bg:#2c293d bold noreverse",
                "claude": "#e3a17e",
                "openai": "#8ad4bb",
                "blue": "#86b9e8",
                "purple": "#b69af8",
                "running": "#8ad4bb",
                "waiting": "#e4bd7a",
                "failed": "#ed8c96",
                "completion-menu": "bg:#232431 #dedee5",
                "completion-menu.completion.current": "bg:#39334f #ffffff",
            }),
            before_render=lambda app: self._sync_pane_focus(),
        )
        self.app.ttimeoutlen = 0.1

    @property
    def selected(self):
        return self.host_pane.selected

    @selected.setter
    def selected(self, value):
        self.host_pane.selected = value

    @property
    def scroll_line(self):
        return self.host_pane.scroll_line

    @scroll_line.setter
    def scroll_line(self, value):
        self.host_pane.scroll_line = value

    def invalidate(self):
        if hasattr(self, "app"):
            self.app.invalidate()

    @staticmethod
    def _click(action):
        def handler(event):
            if event.event_type == MouseEventType.MOUSE_UP:
                action()
        return handler

    def _pane_toolbar(self):
        fragments = [("class:brand", " ⚙ Settings ", self._click(self.open_settings)),
                     ("", "│ Files ", self._click(self.toggle_files)), ("class:muted", "│ Split ")]
        for direction, arrow in (("left", "←"), ("right", "→"), ("up", "↑"), ("down", "↓")):
            fragments.append(("", f" {arrow} ", self._click(lambda d=direction: self.split_pane(d))))
        fragments.extend([
            ("", "│ × Close ", self._click(self.close_pane)),
            ("", "│ Mouse ", self._click(self.toggle_mouse)),
            ("class:waiting" if self.notice else "class:muted",
             "\n " + (self.notice or "Ctrl+W then arrow: split · F6: next pane · F2: settings · F3: files · F10: mouse clicks")),
        ])
        return fragments

    MOUSE_ON_NOTICE = "Mouse on: click agents, panes and buttons · F10 returns the mouse to your terminal"
    MOUSE_OFF_NOTICE = "Mouse off: drag to select, copy and paste as usual · F10 or Alt+M makes the sidebar clickable"

    def toggle_mouse(self):
        """Switch between native terminal selection and clickable chrome."""
        self.mouse_enabled = not self.mouse_enabled
        self.notice = self.MOUSE_ON_NOTICE if self.mouse_enabled else self.MOUSE_OFF_NOTICE
        self.invalidate()

    def _rebuild_panes(self):
        def build(node):
            if isinstance(node, Pane):
                return self._pane_widgets[node.id]
            children = [build(node.first),
                        Window(width=1, char="│", style="class:border") if node.axis == "x"
                        else Window(height=1, char="─", style="class:border"), build(node.second)]
            container = VSplit if node.axis == "x" else HSplit
            return container(children, width=Dimension(min=1, preferred=1), height=Dimension(min=1, preferred=1))
        self._pane_tree = build(self.panes.root)
        self.invalidate()

    def _visible_panes(self):
        if not hasattr(self, "app"):
            return self._pane_tree
        size = self.app.output.get_size()
        width = size.columns - (self.SIDEBAR_WIDTH + 1 if self._show_sidebar() else 0)
        rects = self.panes.rectangles(width, size.rows - 2)
        # On a small terminal, keep every process alive and show the active pane.
        self._compact_panes = any(w < 24 or h < 10 for _, _, w, h in rects.values())
        return self._pane_widgets[self.panes.active.id] if self._compact_panes else self._pane_tree

    def _sync_pane_focus(self):
        if self.dialog is not None:
            return
        control = self.app.layout.current_control
        for pane in self.panes.leaves():
            if ((pane is self.host_pane and control in (self.composer.content, self.content))
                    or (pane.terminal and control is pane.terminal.terminal_control)
                    or (pane.file and control is self._pane_widgets[pane.id].children[1].content)):
                self.panes.active = pane
                break

    def focus_pane(self, pane):
        self.panes.active = pane
        if pane.terminal:
            self.app.layout.focus(pane.terminal.terminal_control)
        elif pane.file:
            self.app.layout.focus(self._pane_widgets[pane.id].children[1])
        else:
            self.app.layout.focus(self.buffer)
        self.invalidate()

    def split_pane(self, direction, *, file=None):
        if direction not in {"left", "right", "up", "down"}:
            raise ValueError(f"Unknown split direction: {direction}")
        if len(self.panes.leaves()) >= 8:
            self.notice = "Close a pane before opening another (8 panes maximum)."
            self.invalidate()
            return None
        terminal = None
        if file is None:
            backend = None
            try:
                from ptterm import Terminal
                from .terminal_backend import ShellBackend

                config = self.repl.config
                backend = ShellBackend(self.file_tree.root, config.terminal_shell,
                                       theme=config.terminal_theme, font=config.terminal_font_mode,
                                       theme_file=config.terminal_theme_file)
                terminal = Terminal(backend=backend, done_callback=self.invalidate, style="class:workspace")
            except (ImportError, OSError, RuntimeError, ValueError) as exc:
                if backend is not None:
                    backend.kill()
                if isinstance(exc, ImportError):
                    self.notice = "Shell support is missing. Reinstall Tether with its dependencies."
                else:
                    self.notice = f"Cannot open shell: {clean_label(exc)}"
                self.invalidate()
                return None
        # Only change the layout after shell support and the PTY are ready.
        # An old editable install can have new source but stale dependencies.
        pane = self.panes.split(direction)
        self.notice = ""
        if file is not None:
            pane.file = Path(file)
            pane.preview = file_preview(pane.file)
            bindings = KeyBindings()

            @bindings.add("up")
            @bindings.add("down")
            @bindings.add("pageup")
            @bindings.add("pagedown")
            @bindings.add("home")
            @bindings.add("end")
            def scroll(event):
                count = pane.preview.count("\n")
                key = event.key_sequence[-1].key
                jump = max(1, self.app.output.get_size().rows // 2)
                value = {"up": -1, "down": 1, "pageup": -jump, "pagedown": jump}.get(key, 0)
                pane.scroll_line = (0 if key == "home" else count if key == "end"
                                    else max(0, min(count, (pane.scroll_line or 0) + value)))

            body = Window(FormattedTextControl(lambda: pane.preview, focusable=True,
                          key_bindings=bindings, get_cursor_position=lambda: Point(0, pane.scroll_line or 0)),
                          wrap_lines=True)
        else:
            pane.terminal = body = terminal
        self._pane_widgets[pane.id] = HSplit([
            Window(FormattedTextControl(lambda: self._pane_header(pane)), height=1), body,
        ], width=Dimension(min=1, preferred=1), height=Dimension(min=1, preferred=1))
        self._rebuild_panes()
        self.focus_pane(pane)
        return pane

    def _shell_title(self, pane):
        backend = pane.terminal.process.backend
        name = clean_label(backend.get_name())
        title = clean_label(pane.terminal.process.screen.title or name)
        icon = "›_"
        for match, provider in (("claude", "anthropic"), ("codex", "codex"), ("gpt", "openai")):
            if match in (name + " " + title).lower():
                icon = provider_identity(provider)[0]
                break
        if backend.ready_f.done():
            title = clean_label(backend.error) if backend.error else f"Exited ({backend.exit_code})"
        return icon, title or "Shell"

    def _pane_header(self, pane):
        if pane.file:
            title = f"File · {clean_label(pane.file.name)} · read only"
        else:
            icon, name = self._shell_title(pane)
            title = f"{icon} {name} · Shell {pane.id - 1}"
        style = "class:pane-active" if pane is self.panes.active else "class:muted"
        return [(style, f" {title} ", self._click(lambda: self.focus_pane(pane))),
                (style, " × ", self._click(lambda: self.close_pane(pane)))]

    def close_pane(self, pane=None):
        pane = pane or self.panes.active
        if pane is self.host_pane:
            self.notice = "Select a shell or file pane to close it."
            self.invalidate()
            return
        if pane.terminal:
            backend = pane.terminal.process.backend
            self._closing_shells.add(backend)
            backend.ready_f.add_done_callback(lambda _: self._closing_shells.discard(backend))
            backend.kill()
        self.panes.active = pane
        self.panes.close()
        del self._pane_widgets[pane.id]
        self._rebuild_panes()
        self.focus_pane(self.panes.active)

    def _close_shells(self):
        backends = self._closing_shells | {p.terminal.process.backend for p in self.panes.leaves() if p.terminal}
        for backend in backends:
            backend.kill()
        return backends

    def open_settings(self):
        if self.dialog is not None:
            return
        from .settings_menu import SettingsMenu

        config = self.repl.config
        self._show_dialog(SettingsMenu([
            ("Shell & appearance", "Shell, zsh theme, Powerline and Nerd Font glyphs", self.open_shell_settings),
            ("Project folder", clean_label(self.file_tree.root), self.open_directory_settings),
            ("Model", clean_label(config.api_model or "Local GGUF model"),
             lambda: self._settings_command("/model")),
            ("Reasoning effort", "Choose how much reasoning the model uses",
             lambda: self._settings_command("/effort")),
        ], self.close_dialog))

    def _show_dialog(self, dialog, focus=None):
        self.close_dialog()
        self.dialog = dialog
        self._modal_float = Float(content=dialog.dialog)
        self.root.floats.append(self._modal_float)
        self.app.layout.update_parents_relations()
        self.app.layout.focus(focus or dialog.buttons[0])
        self.invalidate()

    def open_directory_settings(self):
        from .directory_dialog import DirectoryDialog

        dialog = DirectoryDialog(self.file_tree.root, self.file_tree.show_hidden,
                                 self._request_directory, self.close_dialog)
        self._show_dialog(dialog, dialog.path)

    def _settings_command(self, command):
        # Use the existing serialized REPL command path; never change a model
        # while it is working or route a settings action into an approval prompt.
        from ..core.config import reasoning_setting

        message = ""
        if self.directory_busy() or self.repl._broker.has_pending():
            message = "Finish or stop the current task before changing model settings."
        elif command == "/model" and not self.repl.config.is_remote:
            message = "Local models are loaded at startup; this session's model stays loaded."
        elif command == "/effort" and not reasoning_setting(self.repl.config)[1]:
            message = "This model has no configurable reasoning levels."
        if message:
            self.dialog.message = message
            self.invalidate()
            return
        self.close_dialog()
        self.inbox.put(command)

    def open_shell_settings(self):
        from .shell_dialog import ShellDialog

        def save(shell, theme, font, theme_file):
            from .shell_theme import save_settings

            try:
                save_settings(self.repl.config, shell, theme, font, theme_file)
            except (OSError, ValueError) as exc:
                self.dialog.message = clean_label(exc)
                self.invalidate()
                return
            self.close_dialog()
            self.notice = "Shell settings saved. Open a new split to use them."
            self.invalidate()

        dialog = ShellDialog(self.repl.config, save, self.close_dialog)
        self._show_dialog(dialog, dialog.search)

    def open_models(self, models, provider, source):
        self._open_choice_dialog(models, provider, self.repl.config.api_model, source, "model", "model")

    def open_effort(self, choices, current, source):
        _, provider, _ = provider_identity(self.repl.config.provider)
        self._open_choice_dialog([{"id": value, "label": value} for value in choices],
                                 provider, current, source, "effort", "reasoning effort")

    def _open_choice_dialog(self, choices, provider, current, source, command, choice_name):
        """Called by the REPL worker; install the dialog on the UI event loop."""
        from .model_dialog import ModelDialog
        scope = (self.repl.config.provider, self.repl.config.api_model)

        def choose(value):
            self.close_dialog()
            if scope != (self.repl.config.provider, self.repl.config.api_model):
                return
            # Selection goes through the normal idle-command path, so it
            # cannot change a provider's config in the middle of a request.
            self.inbox.put(f"/{command} {value}")

        def show():
            if self.dialog is not None or self.closed.is_set():
                return
            self.dialog = ModelDialog(choices, provider, current, source, choose,
                                      self.close_dialog, choice_name=choice_name)
            self._modal_float = Float(content=self.dialog.dialog)
            self.root.floats.append(self._modal_float)
            self.app.layout.focus(self.dialog.search)
            self.invalidate()

        self.app.loop.call_soon_threadsafe(show)

    def close_dialog(self):
        if self._modal_float is not None:
            self.root.floats.remove(self._modal_float)
        self.dialog = self._modal_float = None
        self.focus_pane(self.panes.active)

    def _request_directory(self, path, show_hidden):
        try:
            target = Path(path).expanduser()
            if not target.is_absolute():
                target = self.dialog.current / target
            target = target.resolve(strict=True)
            if not target.is_dir():
                raise NotADirectoryError(str(target))
        except (OSError, ValueError) as exc:
            self.dialog.message = f"Cannot open directory: {exc}"
            return
        if self.directory_busy():
            self.dialog.message = "Finish or stop Tether's running agents before changing project folders."
            return
        self.file_tree.show_hidden = show_hidden
        self._directory_pending = True
        self.inbox.put(target)
        self.close_dialog()

    def directory_busy(self):
        return self._host_status() != "idle" or any(
            agent.get("status") in {"running", "queued"} for agent in self._agent_rows())

    def directory_changed(self, path=None, error=""):
        """The REPL worker acknowledges a serialized directory change."""
        def apply():
            self._directory_pending = False
            if error:
                self.notice = error
            else:
                self.file_tree.set_root(Path(path))
                self.files_visible = self.sidebar_visible = True
                self.notice = ""
            self.invalidate()
        self.app.loop.call_soon_threadsafe(apply)

    def toggle_files(self):
        self.files_visible = not self.files_visible
        if self.files_visible:
            self.sidebar_visible = True
            self.file_tree.refresh()
            if self._show_sidebar():
                self.app.layout.focus(self.tree_control)
        else:
            self.focus_pane(self.panes.active)
        self.invalidate()

    def _files_header(self):
        return [("class:brand", "  FILES "),
                ("class:muted", "↻", self._click(self.file_tree.refresh)),
                ("class:muted", "\n  Enter open · ←/→ fold")]

    def _tree_bindings(self):
        kb = KeyBindings()
        for key, delta in (("up", -1), ("down", 1), ("pageup", -10), ("pagedown", 10)):
            kb.add(key)(lambda event, d=delta: self.file_tree.move(d))
        kb.add("left")(lambda event: self.file_tree.collapse())
        kb.add("enter")(lambda event: self._open_tree_entry())
        kb.add("right")(lambda event: self._open_tree_entry(expand_only=True))
        kb.add("f5")(lambda event: self.file_tree.refresh())
        kb.add("escape")(lambda event: self.focus_pane(self.panes.active))
        return kb

    def _tree_text(self):
        fragments = []
        for i, entry in enumerate(self.file_tree.rows()):
            if entry.note:
                label = entry.note
            else:
                mark = ("▾" if entry.path in self.file_tree.expanded else "▸") if entry.directory else "·"
                label = f"{mark} {entry.path.name}{'/' if entry.directory else ''}{' ↗' if entry.symlink else ''}"

            def click(event, index=i):
                if event.event_type == MouseEventType.MOUSE_UP:
                    self.file_tree.selected = index
                    self.app.layout.focus(self.tree_control)
                    self._open_tree_entry()

            fragments.append(("class:folder-selected" if i == self.file_tree.selected else "",
                              "  " + "  " * entry.depth + clean_label(label) + "\n", click))
        return fragments or [("class:muted", "  Empty folder")]

    def _open_tree_entry(self, expand_only=False):
        entry = self.file_tree.current()
        if not entry or entry.note:
            return
        if entry.directory:
            if not expand_only or entry.path not in self.file_tree.expanded:
                self.file_tree.toggle(entry.path)
        elif not expand_only:
            pane = next((p for p in self.panes.leaves() if p.file), None)
            if pane:
                pane.file, pane.preview, pane.scroll_line = entry.path, file_preview(entry.path), 0
                self.focus_pane(pane)
            else:
                self.split_pane("right", file=entry.path)
        self.invalidate()

    def _show_sidebar(self):
        return self.sidebar_visible and self.app.output.get_size().columns >= 72

    def _content_width(self):
        if self.content_window.render_info:
            return self.content_window.render_info.window_width
        return self.app.output.get_size().columns - (self.SIDEBAR_WIDTH + 1 if self._show_sidebar() else 0)

    def _composer_height(self):
        width = max(1, self._content_width() - 3)
        rows = sum(max(1, (get_cwidth(line) + width - 1) // width)
                   for line in self.buffer.text.split("\n"))
        return min(7, max(2, rows))

    def update_agent(self, snapshot: dict):
        with self._agent_lock:
            old = self.agents.get(snapshot["agent_id"], {})
            if snapshot.get("revision", 0) >= old.get("revision", -1):
                self.agents[snapshot["agent_id"]] = dict(snapshot)
        self.invalidate()

    def _agent_rows(self):
        with self._agent_lock:
            return sorted(self.agents.values(), key=lambda a: (
                a.get("status") not in {"running", "queued"}, a.get("agent_number", 0)))

    def select(self, agent_id):
        if agent_id.startswith("shell:"):
            pane = next((p for p in self.panes.leaves() if f"shell:{p.id}" == agent_id), None)
            if pane:
                self.focus_pane(pane)
            return
        self.selected = agent_id
        self.scroll_line = None
        self.content_window.vertical_scroll = 0
        self.focus_pane(self.host_pane)
        self.invalidate()

    def _select_relative(self, delta):
        ids = ["host", *(a["agent_id"] for a in self._agent_rows()),
               *(f"shell:{p.id}" for p in self.panes.leaves() if p.terminal)]
        selected = f"shell:{self.panes.active.id}" if self.panes.active.terminal else self.selected
        index = ids.index(selected) if selected in ids else 0
        self.select(ids[(index + delta) % len(ids)])

    def _host_identity(self):
        cfg = self.repl.config
        return (cfg.provider if cfg.is_remote else "local",
                cfg.api_model if cfg.is_remote else Path(cfg.model_path).stem or "Local model")

    def _host_status(self):
        if self.repl._broker.has_pending():
            return "waiting"
        if self.repl._controller and self.repl._controller.is_busy():
            return "running"
        return "idle"

    @staticmethod
    def _agent_status(agent):
        if agent.get("status") == "running" and agent.get("activity", "").startswith("Waiting for approval"):
            return "waiting"
        return agent.get("status", "queued")

    def _sidebar(self):
        rows = self._agent_rows()
        active = sum(a.get("status") in {"running", "queued"} for a in rows)
        fragments = [("class:brand", "\n  ◇ TETHER"),
                     ("class:muted", "           ⚙", self._click(self.open_settings)), ("", "\n\n"),
                     ("class:muted", "  WORKSPACE\n"),
                     ("", f"  {clean_label(self.file_tree.root.name)[:25]}\n\n", self._click(self.open_settings)),
                     ("class:muted", f"  AGENTS · {active + (self._host_status() == 'running')} running\n\n")]
        provider, model = self._host_identity()
        entries = [("host", "Host", provider, model, self._host_status())]
        entries.extend((a["agent_id"], a.get("label", "Agent"),
                        a.get("provider", provider), a.get("model", model),
                        self._agent_status(a)) for a in rows)
        for pane in self.panes.leaves():
            if pane.terminal:
                icon, title = self._shell_title(pane)
                entries.append((f"shell:{pane.id}", f"Shell {pane.id - 1}", "shell", title,
                                "exited" if pane.terminal.process.backend.ready_f.done() else "open"))
        # Keep the selected row visible even with more agents than screen rows.
        available = min(18, self.app.output.get_size().rows // 2) if self.files_visible else self.app.output.get_size().rows - 3
        capacity = max(1, (available - 8) // 3)
        selection = f"shell:{self.panes.active.id}" if self.panes.active.terminal else self.selected
        selected_index = next((i for i, row in enumerate(entries) if row[0] == selection), 0)
        start = max(0, selected_index - capacity + 1)
        for agent_id, label, provider, model, status in entries[start:start + capacity]:
            icon, name, color = provider_identity(provider, model)
            if provider == "shell":
                pane = next(p for p in self.panes.leaves() if f"shell:{p.id}" == agent_id)
                icon, name, color = self._shell_title(pane)[0], model, "blue"
            selected = " class:agent-selected" if agent_id == selection else ""

            def click(event, target=agent_id):
                if event.event_type == MouseEventType.MOUSE_UP:
                    self.select(target)

            marker = "›" if selected else " "
            fragments.extend([
                (f"class:{color}{selected}", f" {marker} {icon} {clean_label(name + ' · ' + label)[:22]:<22}\n", click),
                (f"class:{status if status in {'running', 'waiting', 'failed'} else 'muted'}{selected}",
                 f"     {clean_label(status + ' · ' + model)[:22]:<22}\n", click),
                ("", "\n", click),
            ])
        if len(entries) > capacity:
            fragments.append(("class:muted", f"  {len(entries)} agents · alt+↑/↓\n"))
        if not rows and len(entries) == 1 and not self.files_visible:
            fragments.append(("class:muted", "  Sub-agents appear here\n  when Tether delegates.\n"))
        return fragments

    def _view_text(self):
        if self.selected == "host":
            if self.transcript.revision != self._cached_revision:
                self._cached_revision = self.transcript.revision
                self._cached_transcript = self.transcript.text()
                self._cached_lines = self._cached_transcript.count("\n") + 1
            return self._cached_transcript
        with self._agent_lock:
            agent = dict(self.agents.get(self.selected, {}))
        lines = [f"  {clean_label(agent.get('label', 'Agent'))} · {clean_label(agent.get('agent_type', ''))}",
                 f"  {clean_label(agent.get('status', ''))} · {agent.get('tokens', 0):,} tokens · {agent.get('elapsed_seconds', 0):.1f}s",
                 "", "  TASK", "  " + str(agent.get("task", "")).replace("\n", "\n  "), "", "  ACTIVITY",
                 "  " + str(agent.get("activity", "")), ""]
        for tool in agent.get("tool_calls", []):
            lines.append(f"  {tool.get('status', '')}  {tool.get('name', '')}  {tool.get('args_preview', '')}")
        if agent.get("output"):
            lines.extend(["", "  RESULT", "  " + agent["output"].replace("\n", "\n  ")])
        return "\n".join(lines)

    def _content_text(self):
        return ANSI(self._view_text())

    def _cursor(self):
        total = self._cached_lines if self.selected == "host" else self._view_text().count("\n") + 1
        line = total - 1 if self.scroll_line is None else min(self.scroll_line, total - 1)
        return Point(0, max(0, line))

    def _scroll(self, delta):
        self.scroll_line = max(0, self._cursor().y + delta)
        if self.scroll_line >= self._view_text().count("\n"):
            self.scroll_line = None

    def _header(self):
        provider, model = self._host_identity()
        if self.selected != "host":
            with self._agent_lock:
                agent = self.agents.get(self.selected, {})
                provider, model = agent.get("provider", provider), agent.get("model", model)
        icon, name, _ = provider_identity(provider, model)
        title = "Conversation" if self.selected == "host" else "Agent activity · Esc to return"
        effort = ""
        if self.selected == "host":
            from ..core.config import reasoning_setting
            field, choices, current = reasoning_setting(self.repl.config)
            if choices or current != "default":
                effort = f" · effort {clean_label(current)}"
        return f"  {title}   /   {icon} {name} · {clean_label(model)}{effort}\n"

    def _activity(self):
        if self.closed.is_set():
            return "  Saving session…"
        if self.repl._broker.has_pending():
            return [("class:waiting", f"  Needs your input · {clean_label(self.repl._broker.hint())}")]
        if self._directory_pending:
            return "  Opening project folder…"
        if self.notice:
            return "  " + self.notice
        queued = len(self.repl._controller.queued()) if self.repl._controller else 0
        suffix = f" · {queued} queued" if queued else ""
        return "  " + (self.stderr.status or self._host_status().capitalize()) + suffix

    def _footer(self):
        engine = self.repl._engine
        tokens = f" · {engine.usage.total:,} tok" if engine else ""
        plan = " · PLAN" if engine and engine.plan_mode else ""
        return f"  Enter send · Alt+Enter newline · PgUp/Dn scroll · Ctrl+C stop{tokens}{plan}"

    def _submit(self, buffer):
        if self.closed.is_set():
            return False
        text = buffer.text
        self.select("host")
        self.transcript.write(f"\n  › {text}\n\n")
        # Fulfill approvals immediately, before another input line can race it.
        if not self.repl._broker.fulfill(text):
            self.inbox.put(text)
        return False

    def read_line(self, timeout=None):
        if self.closed.is_set():
            raise EOFError
        value = self.inbox.get(timeout=timeout)
        if isinstance(value, BaseException):
            raise value
        return value

    def run(self, session):
        result = [0]
        failure = []
        worker = None

        def start():
            nonlocal worker
            loop = asyncio.get_running_loop()

            def work():
                try:
                    result[0] = session()
                except BaseException as exc:
                    failure.append(exc)
                finally:
                    loop.call_soon_threadsafe(finish)

            def finish():
                if self.app.is_running:
                    self.app.exit()

            worker = threading.Thread(target=work, name="tether-repl", daemon=True)
            worker.start()

        # Application's output was created before redirecting Python streams.
        handlers = [(handler, handler.stream) for handler in logging.getLogger("tether").handlers
                    if isinstance(handler, logging.StreamHandler)
                    and not isinstance(handler, logging.FileHandler)]
        for handler, _ in handlers:
            handler.setStream(self.stderr)
        async def run_app():
            try:
                await self.app.run_async(pre_run=start)
            finally:
                pending = [backend.ready_f for backend in self._close_shells()]
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)

        with contextlib.redirect_stdout(self.transcript), contextlib.redirect_stderr(self.stderr):
            try:
                asyncio.run(run_app())
            finally:
                self.closed.set()
                self.repl._broker.cancel()
                self.inbox.put(EOFError())
                if self.repl._controller:
                    self.repl._controller.interrupt()
                if worker:
                    worker.join(timeout=6)
                for handler, stream in handlers:
                    handler.setStream(stream)
        if failure:
            raise failure[0]
        if result[0]:
            print(self.transcript.text(), file=sys.stderr)
        return result[0]
