"""Event history log."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class HistoryEvent:
    timestamp: str
    title: str
    detail: str


@dataclass
class HistoryLog:
    events: list[HistoryEvent] = field(default_factory=list)

    def add(self, title: str, detail: str) -> None:
        self.events.append(HistoryEvent(
            timestamp=datetime.now().isoformat(timespec="seconds"),
            title=title,
            detail=detail,
        ))

    def as_text(self) -> str:
        return "\n".join(
            f"[{e.timestamp}] {e.title}: {e.detail}" for e in self.events
        )
