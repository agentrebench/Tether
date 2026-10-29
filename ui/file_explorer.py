"""Shared, read-only explorer controls for the terminal-first workspace."""
from pathlib import Path

from prompt_toolkit.data_structures import Point
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, VSplit, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.widgets import TextArea

from .file_tree import FileTree, file_preview


def clean(value):
    return "".join(c if c.isprintable() else "?" for c in str(value))


class FileExplorer:
    def __init__(self, workspace):
        self.workspace = workspace
        self.tree = FileTree(workspace.cwd)
        self.path = None
        kb = KeyBindings()
        for key, delta in (("up", -1), ("down", 1), ("pageup", -10), ("pagedown", 10)):
            kb.add(key)(lambda event, delta=delta: self.tree.move(delta))
        kb.add("left")(lambda event: self.tree.collapse())
        kb.add("right")(lambda event: self.open(expand_only=True))
        kb.add("enter")(lambda event: self.open())
        kb.add("f5")(lambda event: self.refresh())
        kb.add(".")(lambda event: self.toggle_hidden())
        kb.add("o")(lambda event: workspace.open_folder())
        kb.add("escape")(lambda event: workspace.focus_terminal())
        kb.add("tab")(lambda event: self.focus_preview())
        self.control = FormattedTextControl(self.rows, focusable=True, key_bindings=kb,
                                           get_cursor_position=lambda: Point(0, self.tree.selected))
        self.sidebar = HSplit([
            Window(FormattedTextControl(self.header), height=2),
            Window(self.control, wrap_lines=False),
            Window(FormattedTextControl(" ↑↓ select · Enter open\n . hidden · O folder · F5 ↻"), height=2,
                   style="class:muted"),
        ], style="class:explorer")
        preview_keys = KeyBindings()
        preview_keys.add("escape")(lambda event: workspace.focus_terminal())
        preview_keys.add("f5")(lambda event: self.refresh())
        preview_keys.add("tab")(lambda event: workspace.focus_files())
        self.preview = TextArea(read_only=True, scrollbar=True, wrap_lines=False,
                                focusable=True, height=None)
        self.preview.control.key_bindings = preview_keys
        self.viewer = HSplit([
            VSplit([
                Window(FormattedTextControl(lambda: " " + clean(self.path.relative_to(self.tree.root))
                                            + " · read only" if self.path else ""), height=1),
                Window(FormattedTextControl(lambda: [("", " × Close ", workspace.click(self.close))]),
                       width=9, height=1),
            ], style="class:bar"),
            self.preview,
        ])

    def header(self):
        click = self.workspace.click
        return [("class:brand", " EXPLORER "), ("", "↻ ", click(self.refresh)),
                ("", "Folder", click(self.workspace.open_folder)),
                ("class:muted", "\n " + clean(self.tree.root.name or self.tree.root))]

    def rows(self):
        rows = []
        for i, entry in enumerate(self.tree.rows()):
            mark = ("▾" if entry.path in self.tree.expanded else "▸") if entry.directory else "·"
            name = entry.note or f"{mark} {entry.path.name}{' ↗' if entry.symlink else ''}"

            def select(index=i):
                self.tree.selected = index
                self.workspace.focus_files()
                self.open()

            rows.append(("class:folder-selected" if i == self.tree.selected else "",
                         " " + "  " * entry.depth + clean(name) + "\n", self.workspace.click(select)))
        return rows or [("class:muted", " Empty folder")]

    def open(self, expand_only=False):
        entry = self.tree.current()
        if not entry or entry.note:
            return
        if entry.directory:
            if expand_only and entry.path in self.tree.expanded:
                rows = self.tree.rows()
                if self.tree.selected + 1 < len(rows) and rows[self.tree.selected + 1].depth > entry.depth:
                    self.tree.move(1)
            else:
                self.tree.toggle(entry.path)
        elif not expand_only:
            self.path = entry.path
            self.preview.text = ("Symbolic link — open its target folder to browse it." if entry.symlink
                                 else file_preview(entry.path, root=self.tree.root)) or "Empty file."
            self.workspace.app.layout.update_parents_relations()
            self.focus_preview()
        self.workspace.invalidate()

    def focus_preview(self):
        if self.path:
            self.workspace.file_focus = "preview"
            self.workspace.app.layout.update_parents_relations()
            self.workspace.app.layout.focus(self.preview)
        else:
            self.workspace.focus_terminal()

    def close(self):
        self.path = None
        self.preview.text = ""
        self.workspace.focus_terminal()
        self.workspace.invalidate()

    def refresh(self):
        selected = self.tree.current()
        self.tree.refresh()
        if selected:
            self.tree.selected = next((i for i, row in enumerate(self.tree.rows())
                                       if row.path == selected.path), 0)
        if self.path:
            self.preview.text = ("Symbolic link — open its target folder to browse it." if self.path.is_symlink()
                                 else file_preview(self.path, root=self.tree.root)) or "Empty file."
        self.workspace.invalidate()

    def toggle_hidden(self):
        self.tree.show_hidden = not self.tree.show_hidden
        self.refresh()

    def set_root(self, path: Path, show_hidden: bool):
        self.tree.set_root(path)
        self.tree.show_hidden = show_hidden
        self.close()
