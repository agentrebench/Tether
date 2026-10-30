"""Terminal-first workspace: real PTYs, optional agent tabs, and theme settings."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from pathlib import Path

from prompt_toolkit.application import Application
from prompt_toolkit.filters import Condition
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import (ConditionalContainer, DynamicContainer, Float, FloatContainer,
                                   HSplit, Layout, VSplit, Window)
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.mouse_events import MouseEventType
from prompt_toolkit.output import ColorDepth
from prompt_toolkit.styles import Style

from .pane_select import PaneSelection, export_clipboard
from .panes import Pane, PaneLayout
from .shell_dialog import ShellDialog
from .shell_theme import save_settings
from .terminal_backend import ShellBackend, tether_command


def label(value):
    return " ".join("".join(c if c.isprintable() else " " for c in str(value)).split())


@dataclass
class TerminalTab:
    id: int
    title: str
    panes: PaneLayout = field(default_factory=PaneLayout)
    widgets: dict = field(default_factory=dict)
    tree: object = None
    draft: tuple | None = None


class ShellWorkspace:
    MAX_TERMINALS = 8

    def __init__(self, config, *, cwd=None, input=None, output=None):
        self.config = config
        self.cwd = Path(cwd or Path.cwd())
        self.tabs = []
        self.active = None
        self.dialog = None
        self.notice = ""
        self.backends = set()
        self._next_id = 1
        self._stopping = False
        # Mouse tracking keeps tabs and buttons clickable; panes provide their
        # own drag-to-copy and click-to-paste (see pane_select). F10, Alt+M,
        # or the Mouse button hands the mouse to the terminal emulator instead.
        self.mouse_enabled = True
        self.empty = Window(FormattedTextControl("Open a terminal with +, or choose Settings."))
        bindings = KeyBindings()
        idle = Condition(lambda: self.dialog is None)
        for key, action in (("f2", self.open_settings), ("f4", self.new_terminal),
                            ("f6", self.next_pane), ("f7", self.next_tab),
                            ("f8", self.split), ("f9", self.new_agent),
                            ("f10", self.toggle_mouse), ("escape m", self.toggle_mouse)):
            bindings.add(*key.split(), filter=idle, eager=True)(lambda event, action=action: action())
        for key in ("escape", "c-c"):
            bindings.add(key, filter=~idle, eager=True)(lambda event: self.close_dialog())
        bindings.add("s-pageup", filter=idle, eager=True)(lambda event: self.scrollback())
        bindings.add("escape", filter=idle & Condition(self.is_scrollback), eager=True)(
            lambda event: self.live_terminal())
        # All ordinary shell shortcuts (including Ctrl+C/D/W/Z, Tab, arrows)
        # belong to the PTY, not to an agent composer or a global prefix map.
        bar = VSplit([
            Window(FormattedTextControl(self.tab_bar), height=1, style="class:bar"),
            Window(FormattedTextControl(self.actions), width=38, height=1, style="class:bar"),
        ])
        self.root = FloatContainer(HSplit([
            bar, DynamicContainer(self.visible_content),
            ConditionalContainer(Window(FormattedTextControl(self.status), height=1, style="class:bar"),
                                 filter=Condition(lambda: bool(self.notice or self.is_scrollback()
                                                                or (self.active and self.active.draft)))),
        ], style="class:terminal"), floats=[])
        self.app = Application(
            layout=Layout(self.root), input=input, output=output, full_screen=True,
            mouse_support=Condition(lambda: self.mouse_enabled), key_bindings=bindings,
            color_depth=ColorDepth.TRUE_COLOR,
            style=Style.from_dict({
                "": "bg:#101418 #dce3e9", "terminal": "bg:#101418 #dce3e9",
                "bar": "bg:#1b2229 #aab8c5", "brand": "#7dd9bf bold",
                "tab-active": "bg:#33434a #e5fff7 bold noreverse",
                "muted": "#91a0ae", "border": "#33434a", "action": "#dce3e9",
                "pane-selection": "reverse",
                "dialog": "bg:#1b2229 #dce3e9", "dialog.body": "bg:#1b2229 #dce3e9",
                "dialog.body text-area": "bg:#101418 #e5fff7",
                "dialog.body text-area last-line": "nounderline",
                "dialog frame.label": "#7dd9bf bold",
                "button.focused": "bg:#33434a #e5fff7 noreverse",
                "radio-selected": "bg:#33434a #e5fff7 noreverse",
                "radio-checked": "#7dd9bf", "shadow": "bg:#090c0f",
            }), before_render=lambda _: self.sync_focus(),
        )
        self.app.ttimeoutlen = .1

    def invalidate(self):
        self.app.invalidate()

    def click(self, action):
        def handler(event):
            if self.dialog is None and event.event_type == MouseEventType.MOUSE_UP:
                action()
        return handler

    def tab_bar(self):
        result = [("class:brand", " TETHER ")]
        tabs = self.tabs
        if self.app.output.get_size().columns < 100 or len(tabs) > 3:
            tabs = [self.active] if self.active else []
            if len(self.tabs) > 1:
                result.append(("", "‹ ", self.click(lambda: self.next_tab(-1))))
        for tab in tabs:
            style = "class:tab-active" if tab is self.active else ""
            result.append((style, f" {tab.id} {label(tab.title)[:14]} ", self.click(lambda t=tab: self.focus_tab(t))))
            result.append((style, "× ", self.click(lambda t=tab: self.close_tab(t))))
        if len(tabs) < len(self.tabs):
            result.append(("", " › ", self.click(self.next_tab)))
        return result

    def actions(self):
        return [("class:action", text, self.click(action)) for text, action in (
            (" + ", self.new_terminal), (" Agent ", self.new_agent),
            (" Split ", self.split), (" Mouse ", self.toggle_mouse), (" Settings ", self.open_settings),
        )]

    MOUSE_ON_NOTICE = "Mouse on: drag to copy · right-click or middle-click pastes · tabs and buttons are clickable"
    MOUSE_OFF_NOTICE = "Mouse off: your terminal selects and copies · F10 or Alt+M brings clicks back"

    def toggle_mouse(self):
        """Hand the mouse to the terminal emulator, or take it back.

        Buttons cannot be clicked while tracking is off, so the notice names
        the key that switches back.
        """
        self.mouse_enabled = not self.mouse_enabled
        self.notice = self.MOUSE_ON_NOTICE if self.mouse_enabled else self.MOUSE_OFF_NOTICE
        self.invalidate()

    def copied(self, text):
        self.app.clipboard.set_text(text)
        export_clipboard(text, self.app.output)
        count = len(text)
        self.notice = f"Copied {count} character{'s' if count != 1 else ''} · right-click or middle-click pastes"
        self.invalidate()

    def paste_into(self, terminal):
        text = self.app.clipboard.get_data().text
        if not text:
            self.notice = "Nothing copied yet: drag across text to copy it."
            self.invalidate()
            return
        terminal.process.write_input(text, paste=True)

    def status(self):
        if self.is_scrollback():
            return [("class:muted", " Scrollback · "),
                    ("class:brand", "Esc / Return to terminal", self.click(self.live_terminal))]
        if self.notice:
            return [("class:muted", " " + label(self.notice))]
        if self.active and self.active.draft:
            return [("class:muted", f" Preview: {label(self.active.title)}  "),
                    ("class:brand", " Use theme ", self.click(self.apply_preview)),
                    ("", " Browse themes ", self.click(self.open_settings))]
        return [("class:muted", " " + label(self.notice))]

    def visible_content(self):
        if not self.active:
            return self.empty
        size = self.app.output.get_size()
        rects = self.active.panes.rectangles(size.columns, size.rows - 1)
        if any(w < 24 or h < 6 for _, _, w, h in rects.values()):
            return self.active.widgets[self.active.panes.active.id]
        return self.active.tree

    def rebuild(self, tab):
        def build(node):
            if isinstance(node, Pane):
                return tab.widgets[node.id]
            vertical = node.axis == "x"
            line = Window(width=1, char="│", style="class:border") if vertical else Window(height=1, char="─", style="class:border")
            return (VSplit if vertical else HSplit)([build(node.first), line, build(node.second)],
                                                    width=Dimension(min=1, preferred=1),
                                                    height=Dimension(min=1, preferred=1))
        tab.tree = build(tab.panes.root)

    def focus_tab(self, tab):
        if tab not in self.tabs:
            return
        self.active = tab
        self.app.layout.update_parents_relations()
        terminal = tab.panes.active.terminal
        self.app.layout.focus(terminal.copy_buffer_control if terminal.is_copying else terminal.terminal_control)
        self.invalidate()

    def sync_focus(self):
        if self.active and not self.dialog:
            for pane in self.active.panes.leaves():
                if self.app.layout.has_focus(pane.terminal.terminal_control) or self.app.layout.has_focus(pane.terminal.copy_buffer_control):
                    self.active.panes.active = pane
                    break

    def next_tab(self, delta=1):
        if self.tabs:
            self.focus_tab(self.tabs[(self.tabs.index(self.active) + delta) % len(self.tabs)])

    def next_pane(self):
        if self.active:
            self.active.panes.cycle()
            self.focus_tab(self.active)

    def is_scrollback(self):
        return bool(self.active and self.active.panes.active.terminal.is_copying)

    def scrollback(self, terminal=None):
        if not self.active:
            return
        terminal = terminal or self.active.panes.active.terminal
        if not terminal.is_copying:
            terminal.enter_copy_mode()
        terminal.copy_buffer.cursor_up(count=max(3, self.app.output.get_size().rows // 2))
        self.invalidate()

    def live_terminal(self):
        if self.is_scrollback():
            self.active.panes.active.terminal.exit_copy_mode()
            self.invalidate()

    def active_directory(self) -> Path:
        """Where the active shell is now; new tabs, splits, and agents start there."""
        if self.active and self.active.panes.active.terminal is not None:
            backend = self.active.panes.active.terminal.process.backend
            current = backend.current_directory() if hasattr(backend, "current_directory") else None
            if current is not None and current.is_dir():
                return current
        return self.cwd

    def _open(self, *, draft=None, agent=False, direction=None):
        if sum(len(tab.panes.leaves()) for tab in self.tabs) >= self.MAX_TERMINALS:
            self.notice = "Eight terminals are open. Close a tab before opening another."
            self.invalidate()
            return None
        from ptterm import Terminal
        config = self.config
        cwd = self.active_directory()
        shell, theme, font, file = draft or (config.terminal_shell, config.terminal_theme,
                                           config.terminal_font_mode, config.terminal_theme_file)
        backend = None
        try:
            backend = ShellBackend(cwd, "" if agent else shell,
                                   theme="inherit" if agent else theme, font=font, theme_file=file,
                                   command=tether_command("chat", "--compact") if agent else None)
            tab = self.active if direction and self.active else TerminalTab(self._next_id, "Agent" if agent else Path(backend.shell).name)
            terminal = Terminal(backend=backend, style="class:terminal",
                                done_callback=lambda: backend.loop.call_soon(self.finished, tab, backend))
            mouse_handler = terminal.terminal_control.mouse_handler
            selection = PaneSelection(terminal, self.copied, self.paste_into)

            def mouse(event):
                screen = terminal.process.screen
                application_mouse = (screen.mouse_support_enabled or screen.sgr_mouse_support_enabled
                                     or screen.urxvt_mouse_support_enabled)
                if application_mouse:
                    mouse_handler(event)
                elif event.event_type == MouseEventType.SCROLL_UP:
                    tab.panes.active = next(p for p in tab.panes.leaves() if p.terminal is terminal)
                    self.scrollback(terminal)
                elif selection.handle(event):
                    if event.event_type == MouseEventType.MOUSE_UP:
                        mouse_handler(event)  # a click still focuses the pane
                    self.invalidate()
                else:
                    mouse_handler(event)
            terminal.terminal_control.mouse_handler = mouse
        except (OSError, ValueError) as exc:
            if backend:
                backend.kill()
            self.notice = f"Cannot open terminal: {label(exc)}"
            self.invalidate()
            return None
        self.backends.add(backend)
        if tab not in self.tabs:
            self._next_id += 1
            self.tabs.append(tab)
        pane = tab.panes.split(direction) if direction and tab.widgets else tab.panes.active
        pane.terminal = terminal
        tab.widgets[pane.id] = terminal
        if draft:
            tab.draft = draft
            tab.title = theme.removeprefix("omz:")
        self.rebuild(tab)
        self.notice = ""
        self.focus_tab(tab)
        return tab

    def new_terminal(self):
        return self._open()

    def new_agent(self):
        return self._open(agent=True)

    def split(self):
        return self._open(direction="right")

    def finished(self, tab, backend):
        backend.kill()
        self.backends.discard(backend)
        if self._stopping or tab not in self.tabs:
            return
        pane = next((p for p in tab.panes.leaves() if p.terminal.terminal_control.process.backend is backend), None)
        if pane is None:
            return
        if len(tab.panes.leaves()) == 1:
            self.close_tab(tab, result=backend.exit_code or 0)
        else:
            tab.panes.active = pane
            tab.panes.close()
            tab.widgets.pop(pane.id)
            self.rebuild(tab)
            if tab is self.active and not self.dialog:
                self.focus_tab(tab)
        if backend.error:
            self.notice = label(backend.error)

    def close_tab(self, tab, result=0):
        if tab not in self.tabs:
            return
        for pane in tab.panes.leaves():
            backend = pane.terminal.terminal_control.process.backend
            backend.kill()
            backend.ready_f.add_done_callback(lambda _, b=backend: self.backends.discard(b))
        self.tabs.remove(tab)
        if tab is self.active:
            self.active = self.tabs[-1] if self.tabs else None
            if self.active and not self.dialog:
                self.focus_tab(self.active)
        if not self.tabs and self.app.is_running and not self.dialog:
            self.app.exit(result=result)
        self.invalidate()

    def open_settings(self):
        if self.dialog:
            return
        config = self.config
        if self.active and self.active.draft:
            shell, theme, font, file = self.active.draft
            config = replace(config, terminal_shell=shell, terminal_theme=theme,
                             terminal_font_mode=font, terminal_theme_file=file)
        self.dialog = ShellDialog(config, self.save_theme, self.close_dialog, self.preview_theme)
        self.root.floats.append(Float(content=self.dialog.dialog))
        self.app.layout.update_parents_relations()
        self.app.layout.focus(self.dialog.search)
        self.invalidate()

    def close_dialog(self):
        self.root.floats.clear()
        self.dialog = None
        if self.active:
            self.focus_tab(self.active)
        self.invalidate()

    def preview_theme(self, *values):
        # Only start code for the explicitly chosen theme, never for search.
        from .shell_theme import validate_settings
        try:
            validate_settings(*values)
        except (OSError, ValueError) as exc:
            self.dialog.message = label(exc)
            return
        # Keep at most one preview process, even while browsing many themes.
        for tab in list(self.tabs):
            if tab.draft:
                self.close_tab(tab)
        tab = self._open(draft=values)
        if tab:
            self.close_dialog()
        else:
            self.dialog.message = self.notice

    def save_theme(self, *values):
        try:
            save_settings(self.config, *values)
        except (OSError, ValueError) as exc:
            if self.dialog:
                self.dialog.message = label(exc)
            else:
                self.notice = label(exc)
            self.invalidate()
            return
        preview = next((tab for tab in self.tabs if tab.draft == values), None)
        self.close_dialog()
        if preview:
            preview.draft = None
            self.focus_tab(preview)
        else:
            self.new_terminal()

    def apply_preview(self):
        if self.active and self.active.draft:
            self.save_theme(*self.active.draft)

    async def run_async(self):
        try:
            return await self.app.run_async(pre_run=self.new_terminal)
        finally:
            self._stopping = True
            for backend in self.backends:
                backend.kill()
            if self.backends:
                await asyncio.wait_for(asyncio.gather(*(b.ready_f for b in self.backends)), timeout=5)
            self.backends.clear()

    def run(self):
        return asyncio.run(self.run_async())
