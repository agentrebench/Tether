"""Automatic learning: turn what the agent just did into durable, cited beliefs.

The design's third criterion for "learned" is *load-bearing*: a belief that no
task ever reads is decoration. That only works if the store fills up as a
by-product of ordinary work — the model cannot be relied on to remember to call
``model_record``. So after every substantive turn we make one small extraction
call: *what did this turn establish about the codebase that would still be
true and useful next week?* Each candidate must cite a slice of the repo; a
claim we cannot point at is discarded, because it could never be re-verified.

Cost model: at most one extra model call per turn, no tools, bounded prompt
(the turn's messages are clipped), and it runs off the hot path — callers
should invoke it in a background thread after the turn's result is delivered.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ..models import Message
from .citations import format_citation, parse as parse_citation
from .model import BeliefKind

_SYSTEM = (
    "You extract durable knowledge about a codebase from an AI coding agent's "
    "completed turn. Return ONLY a JSON array. Each item: "
    '{"kind": "belief" | "invariant" | "decision", "claim": "<one sentence, '
    'stated in its own terms>", "citations": ["<repo-relative path> @ <symbol>", ...], '
    '"confidence": 0.0-1.0}. '
    "Rules: record only facts that will still matter in future sessions — "
    "ownership (what module/class owns what concern), architectural rules the "
    "code or the user enforces (invariant), patterns the user rejected or "
    "decisions made with a reason (decision), non-obvious invariants of the "
    "data flow. Every item needs at least one citation to a file (and symbol "
    "when possible) that was actually read or edited in the turn. Do NOT record "
    "trivia, the task itself, transient state, anything obvious from a file "
    "listing, or anything without a citation. Zero items is a fine answer: []"
)

_MAX_TURN_CHARS = 24_000
_MAX_MSG_CHARS = 2_500
_MAX_ITEMS = 6
_MIN_TOOL_CALLS = 2


def _clip(text: str, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[: limit - 20] + "\n… (clipped)"


def render_turn(messages: list[Message]) -> str:
    """Compact transcript of one turn for the extractor: user prompt, the
    agent's prose, and each tool call with a clipped result. Tool arguments and
    outputs are what carry the citations, so they are kept (bounded)."""
    parts: list[str] = []
    for msg in messages:
        if msg.role == "system":
            continue
        if msg.role == "user":
            parts.append(f"[user]\n{_clip(msg.content or '', _MAX_MSG_CHARS)}")
        elif msg.role == "assistant":
            if msg.content:
                parts.append(f"[assistant]\n{_clip(msg.content, _MAX_MSG_CHARS)}")
            for tc in msg.tool_calls or []:
                try:
                    args = json.dumps(tc.arguments, ensure_ascii=False)
                except TypeError:
                    args = str(tc.arguments)
                parts.append(f"[tool call] {tc.name} {_clip(args, 600)}")
        elif msg.role == "tool":
            parts.append(f"[tool result: {msg.name}]\n{_clip(msg.content or '', 1_200)}")
    text = "\n\n".join(parts)
    return _clip(text, _MAX_TURN_CHARS)


def turn_is_substantive(messages: list[Message], min_tool_calls: int = _MIN_TOOL_CALLS) -> bool:
    calls = sum(len(m.tool_calls or []) for m in messages if m.role == "assistant")
    return calls >= min_tool_calls


_JSON_ARRAY = re.compile(r"\[.*\]", re.S)


def parse_items(text: str) -> list[dict[str, Any]]:
    """Tolerant JSON-array extraction: models wrap the array in prose or
    fences, and reasoning models sometimes run out of output budget mid-array —
    salvage every complete object rather than losing the whole batch."""
    if not text:
        return []
    start = text.find("[")
    if start < 0:
        return []
    candidate = text[start:]
    end = candidate.rfind("]")
    if end >= 0:
        try:
            data = json.loads(candidate[: end + 1])
            if isinstance(data, list):
                return [item for item in data if isinstance(item, dict)]
        except json.JSONDecodeError:
            pass
    # Truncated: decode objects one by one until the text runs out.
    items: list[dict[str, Any]] = []
    decoder = json.JSONDecoder()
    pos = 1
    while True:
        while pos < len(candidate) and candidate[pos] in " \t\r\n,":
            pos += 1
        if pos >= len(candidate) or candidate[pos] != "{":
            break
        try:
            obj, consumed = decoder.raw_decode(candidate, pos)
        except json.JSONDecodeError:
            break
        if isinstance(obj, dict):
            items.append(obj)
        pos = consumed
    return items


def _resolve_citations(model, raw_citations: Any) -> list[str]:
    """Keep only citations that point at a real file in the repo (symbol is
    optional but kept), stamped with the current commit so they survive drift.
    A claim with no surviving citation is not recorded — unverifiable claims
    are exactly the decoration the design forbids."""
    if not isinstance(raw_citations, list):
        return []
    root = Path(model.repo_root)
    try:
        commit = model.indexer.current_commit()
    except Exception:
        commit = ""
    kept: list[str] = []
    for raw in raw_citations[:6]:
        if not isinstance(raw, str) or not raw.strip():
            continue
        try:
            cit = parse_citation(raw.strip())
        except Exception:
            continue
        rel = cit.file.strip().lstrip("./")
        if not rel or ".." in Path(rel).parts:
            continue
        candidate = root / rel
        if not candidate.is_file():
            # The model sometimes cites an absolute path inside the repo.
            try:
                rel = Path(rel).resolve().relative_to(root.resolve()).as_posix()
                candidate = root / rel
            except (ValueError, OSError):
                continue
            if not candidate.is_file():
                continue
        kept.append(format_citation(rel, cit.symbol or "", commit))
    return kept


def learn_from_turn(model, backend, messages: list[Message], *, max_tokens: int = 3000) -> dict[str, Any]:
    """Extract and record durable knowledge from one completed turn.

    Returns a small report ``{"recorded": [...ids], "skipped": n, "reason": str}``.
    Never raises — learning is an enhancement and must not disturb the session.
    """
    report: dict[str, Any] = {"recorded": [], "skipped": 0, "reason": ""}
    try:
        if not turn_is_substantive(messages):
            report["reason"] = "turn too small"
            return report
        transcript = render_turn(messages)
        msg, _usage = backend.chat_completion(
            messages=[
                Message(role="system", content=_SYSTEM),
                Message(role="user", content=f"Turn transcript:\n\n{transcript}\n\nJSON array:"),
            ],
            tools=None,
            max_tokens=max_tokens,
        )
        items = parse_items(msg.content or "")[:_MAX_ITEMS]
        for item in items:
            kind = str(item.get("kind") or "belief").strip().lower()
            claim = " ".join(str(item.get("claim") or "").split())
            if len(claim) < 12:
                report["skipped"] += 1
                continue
            citations = _resolve_citations(model, item.get("citations"))
            if not citations:
                report["skipped"] += 1
                continue
            try:
                confidence = float(item.get("confidence", 0.6))
            except (TypeError, ValueError):
                confidence = 0.6
            confidence = min(0.9, max(0.3, confidence))  # inferred, never certain
            if kind == "invariant":
                inv = model.record_invariant(claim, confidence=confidence, source="learned")
                report["recorded"].append(inv.id)
            elif kind == "decision":
                dec = model.record_decision(reason=claim)
                report["recorded"].append(dec.id)
            else:
                belief = model.record_belief(
                    claim,
                    confidence=confidence,
                    justified_by=citations,
                    source="learned",
                    kind=BeliefKind.DESCRIPTIVE,
                )
                report["recorded"].append(belief.id)
        if not items:
            report["reason"] = "nothing durable in this turn"
    except Exception as exc:  # never let learning break a session
        report["reason"] = f"learning skipped: {exc}"
    return report
