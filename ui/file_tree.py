"""Lazy local file browsing for the terminal workspace."""
from __future__ import annotations

import os
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from itertools import islice
from pathlib import Path


@contextmanager
def _scoped_open(root: Path, path: Path, directory=False):
    """Walk from an open root without following replaced directories/symlinks."""
    parts = path.relative_to(root).parts
    if ".." in parts:
        raise ValueError("Path is outside the selected folder")
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for i, part in enumerate(parts):
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            if directory or i < len(parts) - 1:
                flags |= os.O_DIRECTORY
            mode = os.stat(part, dir_fd=fd, follow_symlinks=False).st_mode
            if not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
                raise ValueError("Symbolic links and special files cannot be previewed")
            next_fd = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        yield fd
    finally:
        os.close(fd)


@dataclass(frozen=True)
class FileEntry:
    path: Path
    depth: int
    directory: bool = False
    symlink: bool = False
    note: str = ""


class FileTree:
    MAX_ENTRIES = 500
    MAX_ROWS = 2000
    MAX_DIRECTORIES = 128

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.expanded: set[Path] = {self.root}
        self.show_hidden = False
        self.selected = 0
        self._cache: dict[Path, list[FileEntry]] = {}
        self._rows: list[FileEntry] | None = None

    def set_root(self, root: Path):
        root = root.expanduser().resolve(strict=True)
        if not root.is_dir():
            raise NotADirectoryError(str(root))
        self.root = root
        self.expanded = {root}
        self.selected = 0
        self.refresh()

    def refresh(self):
        self._cache.clear()
        self._rows = None

    def children(self, path: Path) -> list[FileEntry]:
        if path not in self._cache:
            try:
                rows = []
                clipped = False
                with _scoped_open(self.root, path, directory=True) as fd, os.scandir(fd) as entries:
                    for scanned, entry in enumerate(islice(entries, 5001)):
                        if scanned == 5000:
                            clipped = True
                            break
                        if not self.show_hidden and entry.name.startswith("."):
                            continue
                        if len(rows) == self.MAX_ENTRIES:
                            clipped = True
                            break
                        try:
                            rows.append(FileEntry(path / entry.name, 0, entry.is_dir(follow_symlinks=False), entry.is_symlink()))
                        except OSError:
                            rows.append(FileEntry(path / entry.name, 0, note="unavailable"))
                rows.sort(key=lambda entry: (not entry.directory, entry.path.name.casefold()))
                if clipped:
                    rows.append(FileEntry(path, 0, note=f"Listing limited to {self.MAX_ENTRIES} entries / 5000 scanned"))
            except (OSError, ValueError) as exc:
                rows = [FileEntry(path, 0, note=str(exc))]
            if len(self._cache) >= self.MAX_DIRECTORIES:
                self._cache.pop(next(iter(self._cache)))
            self._cache[path] = rows
        return self._cache[path]

    def rows(self) -> list[FileEntry]:
        if self._rows is None:
            rows = []

            def walk(directory, depth):
                for entry in self.children(directory):
                    if len(rows) >= self.MAX_ROWS:
                        return
                    rows.append(FileEntry(entry.path, depth, entry.directory, entry.symlink, entry.note))
                    if entry.directory and entry.path in self.expanded and depth < 30:
                        walk(entry.path, depth + 1)

            walk(self.root, 0)
            self._rows = rows
        self.selected = min(self.selected, max(0, len(self._rows) - 1))
        return self._rows

    def current(self) -> FileEntry | None:
        rows = self.rows()
        return rows[self.selected] if rows else None

    def move(self, delta):
        self.selected = max(0, min(self.selected + delta, len(self.rows()) - 1))

    def toggle(self, path: Path):
        if path in self.expanded:
            self.expanded.remove(path)
        elif len(self.expanded) < self.MAX_DIRECTORIES:
            self.expanded.add(path)
        self._rows = None

    def collapse(self):
        entry = self.current()
        if not entry:
            return
        if entry.directory and entry.path in self.expanded:
            self.toggle(entry.path)
        else:
            for i, row in enumerate(self.rows()):
                if row.path == entry.path.parent:
                    self.selected = i
                    break


def file_preview(path: Path, max_bytes: int = 256_000, max_lines: int = 2000, *, root: Path | None = None) -> str:
    """Read a bounded regular text file; never block on devices or FIFOs."""
    try:
        if root is None and not stat.S_ISREG(path.stat().st_mode):
            return "This is not a regular file."
        if root is not None:
            with _scoped_open(root, path) as fd:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    return "This is not a regular file."
                with os.fdopen(os.dup(fd), "rb") as stream:
                    data = stream.read(max_bytes + 1)
        else:
            with os.fdopen(os.open(path, os.O_RDONLY | os.O_NONBLOCK), "rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    return "This is not a regular file."
                data = stream.read(max_bytes + 1)
        if b"\0" in data:
            return "Binary file — text preview unavailable."
        text = data[:max_bytes].decode("utf-8", errors="replace")
        # Display control bytes literally; file contents cannot style the UI.
        text = "".join(c if c in "\n\t" or (ord(c) >= 32 and not 127 <= ord(c) < 160)
                       else f"\\x{ord(c):02x}" for c in text)
        lines = text.expandtabs(4).splitlines()
        preview = "\n".join(f"{i:4}  {line}" for i, line in enumerate(lines[:max_lines], 1))
        if len(data) > max_bytes or len(lines) > max_lines:
            preview += "\n\n… Preview truncated."
        return preview or "Empty file."
    except (OSError, ValueError) as exc:
        return f"Cannot preview this file: {exc}"
