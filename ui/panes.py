"""Directional pane layout, independent of terminal rendering."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class Pane:
    id: int
    selected: str = "host"
    scroll_line: int | None = None
    file: Path | None = None
    preview: str = ""
    terminal: Any = None


@dataclass
class Split:
    axis: str  # x = left/right, y = up/down
    first: Pane | Split
    second: Pane | Split


class PaneLayout:
    def __init__(self):
        self.root: Pane | Split = Pane(1)
        self.active = self.root
        self._next_id = 2

    def leaves(self, node=None) -> list[Pane]:
        node = self.root if node is None else node
        if isinstance(node, Pane):
            return [node]
        return self.leaves(node.first) + self.leaves(node.second)

    def _replace(self, target, replacement, node=None):
        node = self.root if node is None else node
        if node is target:
            return replacement
        if isinstance(node, Split):
            node.first = self._replace(target, replacement, node.first)
            node.second = self._replace(target, replacement, node.second)
        return node

    def split(self, direction: str) -> Pane:
        if direction not in {"left", "right", "up", "down"}:
            raise ValueError(f"Unknown split direction: {direction}")
        pane = Pane(self._next_id)
        self._next_id += 1
        first, second = ((pane, self.active) if direction in {"left", "up"} else (self.active, pane))
        branch = Split("x" if direction in {"left", "right"} else "y", first, second)
        self.root = self._replace(self.active, branch)
        self.active = pane
        return pane

    def close(self) -> bool:
        if isinstance(self.root, Pane):
            return False
        target = self.active
        remaining = [pane for pane in self.leaves() if pane is not target]

        def remove(node):
            if node is target:
                return None
            if isinstance(node, Pane):
                return node
            first, second = remove(node.first), remove(node.second)
            if first is None:
                return second
            if second is None:
                return first
            return Split(node.axis, first, second)

        self.root = remove(self.root)
        self.active = remaining[0]
        return True

    def cycle(self, delta=1):
        panes = self.leaves()
        self.active = panes[(panes.index(self.active) + delta) % len(panes)]

    def rectangles(self, width, height, node=None, x=0, y=0):
        node = self.root if node is None else node
        if isinstance(node, Pane):
            return {node.id: (x, y, width, height)}
        if node.axis == "x":
            half = max(0, (width - 1) // 2)
            return {**self.rectangles(half, height, node.first, x, y),
                    **self.rectangles(max(0, width - half - 1), height, node.second, x + half + 1, y)}
        half = max(0, (height - 1) // 2)
        return {**self.rectangles(width, half, node.first, x, y),
                **self.rectangles(width, max(0, height - half - 1), node.second, x, y + half + 1)}
