"""Per-line rationale capture — the data layer behind the "why" feature (#4).

When the model writes an edit it may attach a `line_rationale` map: short
explanations of why specific lines are the way they are. We resolve those
explanations to concrete line numbers in the edited file and stash them here so
the user can later ask `/why <file>:<line>` during review and get the model's
reasoning for that exact line instead of guessing.

Line numbers drift as a file keeps changing, so each entry also remembers the
line's code; lookups fall back to relocating by content when the stored line no
longer matches.
"""
from __future__ import annotations

from difflib import SequenceMatcher
from pathlib import Path


def _resolve(file_path: str) -> str:
    try:
        return str(Path(file_path).expanduser().resolve())
    except OSError:
        return str(Path(file_path).expanduser())


def compute_entries(original: str, updated: str, line_rationale: dict) -> list[dict]:
    """Match the model's {line-snippet -> why} map onto the lines that this edit
    actually added/changed, returning [{"line", "code", "rationale"}] with
    1-based line numbers into `updated`.

    Matching is by substring: a rationale key matches an added line when the
    key's trimmed text appears in that line. Each key annotates the first added
    line it matches, so duplicate code doesn't smear one rationale everywhere.
    """
    if not isinstance(line_rationale, dict) or not line_rationale:
        return []

    old_lines = original.splitlines()
    new_lines = updated.splitlines()

    # Line numbers (1-based) of lines present in `updated` but new/changed.
    added: list[tuple[int, str]] = []
    for tag, _i1, _i2, j1, j2 in SequenceMatcher(None, old_lines, new_lines).get_opcodes():
        if tag in ("insert", "replace"):
            for j in range(j1, j2):
                added.append((j + 1, new_lines[j]))

    entries: list[dict] = []
    used_lines: set[int] = set()
    # Stable iteration over the model-supplied map.
    for key, why in line_rationale.items():
        key_s = str(key).strip()
        why_s = str(why).strip()
        if len(key_s) < 2 or not why_s:
            continue
        for line_no, text in added:
            if line_no in used_lines:
                continue
            if key_s in text:
                entries.append({"line": line_no, "code": text.strip(), "rationale": why_s})
                used_lines.add(line_no)
                break
    entries.sort(key=lambda e: e["line"])
    return entries


class RationaleStore:
    """In-memory, per-session store of line rationales, keyed by absolute path."""

    def __init__(self) -> None:
        self._by_file: dict[str, dict[int, dict]] = {}

    def record(self, file_path: str, entries: list[dict]) -> None:
        if not entries:
            return
        bucket = self._by_file.setdefault(_resolve(file_path), {})
        for e in entries:
            bucket[int(e["line"])] = {"code": e["code"], "rationale": e["rationale"]}

    def entries_for(self, file_path: str) -> dict[int, dict]:
        return self._by_file.get(_resolve(file_path), {})

    def lookup(self, file_path: str, line: int) -> dict | None:
        """Resolve the rationale for a (file, line).

        Returns a dict {"rationale", "code", "line", "moved", "actual_line"} or
        None. `moved` is True when the stored line number no longer matches the
        file and we relocated the rationale by its code content.
        """
        bucket = self.entries_for(file_path)
        if not bucket:
            return None

        current = self._read_lines(file_path)

        # 1. Exact line hit whose code still matches the file.
        entry = bucket.get(int(line))
        if entry is not None:
            cur = current[line - 1].strip() if 0 <= line - 1 < len(current) else None
            moved = cur is not None and cur != entry["code"]
            return {
                "rationale": entry["rationale"],
                "code": entry["code"],
                "line": line,
                "moved": False,
                "actual_line": line,
            } if not moved else self._relocate_or_keep(entry, current, line)

        # 2. No entry at that line, but maybe an annotated line drifted onto it.
        if 0 <= line - 1 < len(current):
            cur = current[line - 1].strip()
            for ln, e in bucket.items():
                if e["code"] == cur:
                    return {
                        "rationale": e["rationale"],
                        "code": e["code"],
                        "line": ln,
                        "moved": ln != line,
                        "actual_line": line,
                    }
        return None

    def _relocate_or_keep(self, entry: dict, current: list[str], asked_line: int) -> dict:
        actual = None
        for idx, text in enumerate(current):
            if text.strip() == entry["code"]:
                actual = idx + 1
                break
        return {
            "rationale": entry["rationale"],
            "code": entry["code"],
            "line": asked_line,
            "moved": True,
            "actual_line": actual,
        }

    @staticmethod
    def _read_lines(file_path: str) -> list[str]:
        try:
            return Path(file_path).expanduser().read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
