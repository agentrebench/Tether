"""Drag to select text in a terminal pane; release copies, right or middle click pastes.

Mouse tracking keeps tabs, buttons, and wheel scrollback clickable, but it also
takes the mouse away from the terminal emulator, so plain dragging no longer
selects text. This gives the pane its own selection, the way tmux and PuTTY
do: the copied text goes to the application clipboard (right/middle click
pastes it into any pane, including over SSH), to the host terminal through
OSC 52 when it allows that, and to a local clipboard tool when one exists.
Panes whose program asked for the mouse itself (vim, htop) are left alone.
"""
from __future__ import annotations

import base64
import os
import platform
import shutil
import subprocess
import threading

from prompt_toolkit.layout.controls import UIContent
from prompt_toolkit.mouse_events import MouseButton, MouseEventType

SELECTION_STYLE = "class:pane-selection"
OSC52_LIMIT = 100_000  # bytes of base64; larger payloads are dropped by most terminals


class PaneSelection:
    def __init__(self, terminal, on_copy, on_paste):
        self.terminal = terminal
        self.on_copy = on_copy
        self.on_paste = on_paste
        self.start = self.end = None
        self.pressed = False
        control = terminal.terminal_control
        self._create_content = control.create_content
        control.create_content = self.create_content

    @property
    def bounds(self):
        if self.start is None or self.end is None or self.start == self.end:
            return None
        return min(self.start, self.end), max(self.start, self.end)

    def handle(self, event) -> bool:
        """True when the event belonged to selection or paste."""
        point = (event.position.y, event.position.x)
        kind = event.event_type
        if kind == MouseEventType.MOUSE_DOWN:
            if event.button in (MouseButton.RIGHT, MouseButton.MIDDLE):
                return True
            self.start = self.end = point
            self.pressed = True
            return True
        if kind == MouseEventType.MOUSE_MOVE:
            if not self.pressed:
                return False
            self.end = point
            return True
        if kind == MouseEventType.MOUSE_UP:
            if event.button in (MouseButton.RIGHT, MouseButton.MIDDLE):
                self.on_paste(self.terminal)
                return True
            if not self.pressed:
                return False
            self.pressed = False
            self.end = point
            text = self.text()
            self.start = self.end = None
            if text:
                self.on_copy(text)
            return True
        return False

    def text(self) -> str:
        bounds = self.bounds
        if not bounds:
            return ""
        (y1, x1), (y2, x2) = bounds
        data = self.terminal.process.screen.pt_screen.data_buffer
        lines = []
        for y in range(y1, y2 + 1):
            row = data.get(y)
            if not row:
                lines.append("")
                continue
            low = x1 if y == y1 else 0
            high = x2 if y == y2 else max(row)
            cells = (row.get(x) for x in range(low, high + 1))
            lines.append("".join(cell.char if cell is not None else " " for cell in cells).rstrip())
        return "\n".join(lines).strip("\n")

    def create_content(self, width, height):
        content = self._create_content(width, height)
        bounds = self.bounds
        if not bounds:
            return content
        (y1, x1), (y2, x2) = bounds
        get_line = content.get_line

        def highlighted(number):
            fragments = get_line(number)
            if not y1 <= number <= y2:
                return fragments
            low = x1 if number == y1 else 0
            high = x2 if number == y2 else len(fragments)
            # ptterm yields one fragment per screen cell, so the index is the column.
            return [(f"{fragment[0]} {SELECTION_STYLE}" if low <= index <= high else fragment[0], *fragment[1:])
                    for index, fragment in enumerate(fragments)]

        return UIContent(get_line=highlighted, line_count=content.line_count,
                         cursor_position=content.cursor_position, menu_position=content.menu_position,
                         show_cursor=content.show_cursor)


def clipboard_tool() -> list[str] | None:
    if platform.system() == "Darwin" and shutil.which("pbcopy"):
        return ["pbcopy"]
    if os.environ.get("WAYLAND_DISPLAY") and shutil.which("wl-copy"):
        return ["wl-copy"]
    if os.environ.get("DISPLAY"):
        if shutil.which("xclip"):
            return ["xclip", "-selection", "clipboard"]
        if shutil.which("xsel"):
            return ["xsel", "--clipboard", "--input"]
    return None


def export_clipboard(text: str, output, tool=None) -> None:
    """Best effort only: the application clipboard is the one that always works."""
    payload = base64.b64encode(text.encode("utf-8")).decode("ascii")
    if len(payload) <= OSC52_LIMIT:
        try:
            output.write_raw(f"\x1b]52;c;{payload}\x07")
            output.flush()
        except Exception:
            pass
    tool = clipboard_tool() if tool is None else tool
    if tool:
        def run():
            try:
                subprocess.run(tool, input=text.encode("utf-8"), timeout=3,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
            except (OSError, subprocess.SubprocessError):
                pass
        threading.Thread(target=run, name="tether-clipboard", daemon=True).start()
