from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import os

import pytest

from tether.ui.file_tree import FileTree, file_preview
from tether.ui.panes import PaneLayout
from tether.ui.repl import TetherREPL


def test_tree_expands_lazily_sorts_folders_and_does_not_follow_symlink_loops(tmp_path):
    folder = tmp_path / "src"
    folder.mkdir()
    (folder / "main.py").write_text("print('hello')\n")
    (folder / "loop").symlink_to(tmp_path, target_is_directory=True)
    (tmp_path / "README.md").write_text("Hello")
    (tmp_path / ".hidden").write_text("hidden")
    tree = FileTree(tmp_path)
    assert [e.path.name for e in tree.rows()] == ["src", "README.md"]
    assert folder not in tree._cache
    tree.toggle(folder)
    assert [e.path.name for e in tree.rows()] == ["src", "loop", "main.py", "README.md"]
    assert tree.rows()[1].symlink and not tree.rows()[1].directory
    tree.show_hidden = True
    tree.refresh()
    assert ".hidden" in [e.path.name for e in tree.rows()]
    tree.selected = 2
    tree.collapse()
    assert tree.current().path == folder
    tree.collapse()
    assert "main.py" not in [e.path.name for e in tree.rows()]


def test_tree_handles_deleted_folders_and_bounded_previews(tmp_path):
    folder = tmp_path / "deleted"
    folder.mkdir()
    tree = FileTree(tmp_path)
    tree.toggle(folder)
    folder.rmdir()
    assert tree.children(folder)[0].note
    file = tmp_path / "sample.txt"
    file.write_bytes(b"first\n\x1b[31msecond\nthird\n")
    preview = file_preview(file, max_lines=2)
    assert "\\x1b[31msecond" in preview and "Preview truncated" in preview
    assert "\x1b" not in preview
    file.write_bytes(b"\0binary")
    assert "Binary file" in file_preview(file)
    assert "not a regular file" in file_preview(tmp_path)
    with pytest.raises(NotADirectoryError):
        tree.set_root(file)


def test_scoped_preview_and_tree_refuse_replaced_directory_symlinks(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    folder = root / "src"
    folder.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "private.txt").write_text("PRIVATE_CONTENT")
    tree = FileTree(root)
    tree.toggle(folder)
    tree.rows()
    folder.rmdir()
    folder.symlink_to(outside, target_is_directory=True)
    tree.refresh()
    assert tree.children(folder)[0].note
    assert "PRIVATE_CONTENT" not in file_preview(folder / "private.txt", root=root)
    assert "Cannot preview" in file_preview(outside / "private.txt", root=root)
    fifo = root / "fifo"
    os.mkfifo(fifo)
    assert "Cannot preview" in file_preview(fifo, root=root)


def test_tree_bounds_cached_and_expanded_directories(tmp_path):
    tree = FileTree(tmp_path)
    for i in range(tree.MAX_DIRECTORIES + 10):
        folder = tmp_path / str(i)
        folder.mkdir()
        tree.toggle(folder)
        tree.children(folder)
    assert len(tree.expanded) == len(tree._cache) == tree.MAX_DIRECTORIES


@pytest.mark.parametrize("direction,axis,before", [
    ("left", "x", True), ("right", "x", False), ("up", "y", True), ("down", "y", False),
])
def test_directional_splits_and_closing_preserve_other_panes(direction, axis, before):
    layout = PaneLayout()
    host = layout.active
    shell = layout.split(direction)
    assert layout.root.axis == axis
    assert (layout.root.first is shell) == before
    assert set(layout.rectangles(120, 40)) == {host.id, shell.id}
    nested = layout.split("down")
    layout.close()
    assert nested not in layout.leaves() and shell in layout.leaves()
    layout.active = shell
    layout.close()
    assert layout.root is host and not layout.close()


def test_directory_change_updates_engine_shell_and_session(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "new project"
    target.mkdir()
    repl = object.__new__(TetherREPL)
    bash = Mock()
    repl.tool_registry = {"bash": bash}
    repl.config = SimpleNamespace(codebase_model_enabled=False)
    repl.session = SimpleNamespace(cwd=str(tmp_path))
    repl._workspace = Mock()
    repl._workspace.directory_busy.return_value = False
    engine = Mock(persistent_context_enabled=True)
    repl._change_directory(target, engine)
    assert Path.cwd() == target and repl.session.cwd == str(target)
    bash.set_cwd.assert_called_once_with(str(target))
    engine.set_persistent_context_enabled.assert_called_once_with(True)
    repl._workspace.directory_changed.assert_called_once_with(target)
    repl._change_directory(target / "missing", engine)
    assert Path.cwd() == target
    assert "Cannot open directory" in repl._workspace.directory_changed.call_args.kwargs["error"]
