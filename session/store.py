"""Session persistence — JSON-backed file store."""
from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ..core.config import SESSIONS_DIR


@dataclass
class StoredSession:
    session_id: str
    messages: list[dict] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    cwd: str = ""

    @classmethod
    def new(cls, cwd: str = "") -> StoredSession:
        return cls(session_id=uuid.uuid4().hex[:12], cwd=cwd)


def save_session(session: StoredSession, directory: Path | None = None) -> Path:
    target_dir = directory or SESSIONS_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / f"{session.session_id}.json"
    path.write_text(json.dumps(asdict(session), indent=2))
    return path


def load_session(session_id: str, directory: Path | None = None) -> StoredSession | None:
    target_dir = directory or SESSIONS_DIR
    path = target_dir / f"{session_id}.json"
    if not path.exists():
        return None
    data = json.loads(path.read_text())
    return StoredSession(**data)


def list_sessions(directory: Path | None = None) -> list[StoredSession]:
    target_dir = directory or SESSIONS_DIR
    if not target_dir.exists():
        return []
    sessions = []
    for path in sorted(target_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            data = json.loads(path.read_text())
            sessions.append(StoredSession(**data))
        except (json.JSONDecodeError, TypeError):
            continue
    return sessions
