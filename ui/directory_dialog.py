"""Keyboard- and mouse-accessible project folder picker."""
from pathlib import Path

from prompt_toolkit.completion import PathCompleter
from prompt_toolkit.data_structures import Point
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, VSplit, Window
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.mouse_events import MouseEventType
from prompt_toolkit.widgets import Button, Checkbox, Dialog, Label, TextArea

from .file_tree import FileTree


class DirectoryDialog:
    def __init__(self, root, show_hidden, on_open, on_close, on_shell=None):
        self.current = Path(root)
        self.entries = []
        self.index = 0
        self.message = ""
        self.on_open = on_open
        self.path = TextArea(text=str(root), multiline=False,
                             completer=PathCompleter(only_directories=True, expanduser=True),
                             accept_handler=lambda _: self.browse())
        self.hidden = Checkbox("Show hidden files", checked=show_hidden)
        self.hidden.show_scrollbar = False
        kb = KeyBindings()

        @kb.add("up")
        def up(event):
            self.index = max(0, self.index - 1)

        @kb.add("down")
        def down(event):
            self.index = min(max(0, len(self.entries) - 1), self.index + 1)

        @kb.add("enter")
        def enter(event):
            if self.entries:
                self.navigate(self.entries[self.index])

        self.list_control = FormattedTextControl(self._rows, focusable=True,
                                                get_cursor_position=lambda: Point(0, self.index),
                                                key_bindings=kb)
        self.dialog = Dialog(
            title="⚙ Settings · Project folder", width=Dimension(min=30, preferred=76, max=76),
            body=HSplit([
                Label("Browse folders or type a path. Enter or click a folder to navigate."),
                self.path,
                VSplit([Button("Parent", handler=lambda: self.navigate(self.current.parent)),
                        Button("Browse", handler=self.browse)], padding=1),
                Window(self.list_control, height=Dimension(min=1, preferred=9, max=9), style="class:folder-list"),
                self.hidden,
                Label(lambda: self.message),
            ], padding=1, width=Dimension(preferred=72)),
            buttons=[Button("Open folder", width=16, handler=self.open),
                     *([Button("Shell theme", width=14, handler=on_shell)] if on_shell else []),
                     Button("Cancel", handler=on_close)],
            with_background=False,
        )
        self.navigate(root)

    def navigate(self, path):
        try:
            path = Path(path).expanduser()
            if not path.is_absolute():
                path = self.current / path
            path = path.resolve(strict=True)
            if not path.is_dir():
                raise NotADirectoryError(str(path))
            tree = FileTree(path)
            tree.show_hidden = self.hidden.checked
            children = tree.children(path)
            self.entries = [path.parent] + [entry.path for entry in children if entry.directory]
            self.message = next((entry.note for entry in children if entry.note), "")
            self.current = path
            self.path.text = str(path)
            self.index = 0
        except (OSError, ValueError) as exc:
            self.message = f"Cannot open directory: {exc}"

    def browse(self):
        self.navigate(self.path.text)
        return True  # preserve the path field after Enter

    def open(self):
        self.on_open(self.path.text, self.hidden.checked)

    def _rows(self):
        fragments = []
        for i, path in enumerate(self.entries):
            def click(event, target=path):
                if event.event_type == MouseEventType.MOUSE_UP:
                    self.navigate(target)

            label = ".. /" if i == 0 else path.name + "/"
            label = "".join(c if c.isprintable() else "?" for c in label)
            fragments.append(("class:folder-selected" if i == self.index else "",
                              f"  {label}\n", click))
        return fragments or [("class:muted", "  No subfolders")]
