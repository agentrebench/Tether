"""Durable task checklist tool shared by terminal and desktop clients."""
from __future__ import annotations

import json
import os
import stat
import tempfile
import threading
from pathlib import Path
from typing import Callable

from ..core.models import ToolResult
from .base import BaseTool


VALID_STATUSES = {"pending", "in_progress", "completed"}


class TodoStore:
    """Small owner-private JSON store with atomic replacement."""

    def __init__(self, path: Path):
        self.path = path

    def load(self) -> list[dict]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, ValueError, TypeError):
            return []
        return value if isinstance(value, list) else []

    def save(self, todos: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, temporary = tempfile.mkstemp(
            prefix=f".{self.path.name}.", dir=self.path.parent, text=True
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(todos, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, stat.S_IRUSR | stat.S_IWUSR)
            os.replace(temporary, self.path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


class TodoWriteTool(BaseTool):
    def __init__(
        self,
        *,
        store: TodoStore | None = None,
        on_update: Callable[[list[dict]], None] | None = None,
        initial: list[dict] | None = None,
    ):
        self.store = store
        self.on_update = on_update
        self._lock = threading.RLock()
        source = store.load() if store is not None else (initial or [])
        try:
            self._todos = self._normalize(source)
        except ValueError:
            self._todos = []

    @property
    def name(self) -> str:
        return "todo_write"

    @property
    def description(self) -> str:
        return (
            "Replace the current task checklist. Use it for multi-step work, update "
            "statuses as work progresses, and keep at most one item in progress."
        )

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "todos": {
                    "type": "array",
                    "maxItems": 50,
                    "items": {
                        "type": "object",
                        "properties": {
                            "content": {"type": "string"},
                            "status": {
                                "type": "string",
                                "enum": ["pending", "in_progress", "completed"],
                            },
                            "active_form": {
                                "type": "string",
                                "description": "Present-tense activity shown while in progress.",
                            },
                        },
                        "required": ["content", "status"],
                    },
                }
            },
            "required": ["todos"],
        }

    @staticmethod
    def _normalize(raw: object) -> list[dict]:
        if not isinstance(raw, list):
            raise ValueError("todos must be an array")
        if len(raw) > 50:
            raise ValueError("todos cannot contain more than 50 items")
        todos: list[dict] = []
        in_progress = 0
        for index, item in enumerate(raw):
            if not isinstance(item, dict):
                raise ValueError(f"todos[{index}] must be an object")
            content = str(item.get("content", "")).strip()
            status = str(item.get("status", "")).strip()
            active_form = str(item.get("active_form", "")).strip()
            if not content:
                raise ValueError(f"todos[{index}].content is required")
            if status not in VALID_STATUSES:
                raise ValueError(f"todos[{index}].status is invalid")
            if status == "in_progress":
                in_progress += 1
            todo = {"content": content, "status": status}
            if active_form:
                todo["active_form"] = active_form
            todos.append(todo)
        if in_progress > 1:
            raise ValueError("Only one todo may be in_progress")
        return todos

    def snapshot(self) -> list[dict]:
        with self._lock:
            return [dict(item) for item in self._todos]

    def clear(self) -> None:
        self._commit([])

    def _commit(self, todos: list[dict]) -> None:
        with self._lock:
            self._todos = [dict(item) for item in todos]
            snapshot = self.snapshot()
            if self.store is not None:
                self.store.save(snapshot)
        if self.on_update is not None:
            self.on_update(snapshot)

    def execute(self, arguments: dict) -> ToolResult:
        try:
            todos = self._normalize(arguments.get("todos"))
            self._commit(todos)
        except (OSError, ValueError) as exc:
            return ToolResult(
                name=self.name, content=f"Invalid todo update: {exc}",
                is_error=True, error_code="INVALID_TODOS", display_kind="todo",
            )

        if not todos:
            summary = "Checklist cleared."
        else:
            counts = {status: 0 for status in VALID_STATUSES}
            for item in todos:
                counts[item["status"]] += 1
            summary = (
                f"Checklist updated: {counts['completed']} completed, "
                f"{counts['in_progress']} in progress, {counts['pending']} pending."
            )
        return ToolResult(
            name=self.name, content=summary, display_kind="todo",
            metadata={"todos": todos},
        )
