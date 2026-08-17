"""Self-learning — notice when you repeat a multi-step workflow and prompt
saving it as a real skill (procedural memory).

Opt-in (config.self_learning). When on, every completed multi-step submission
is recorded as an *episode*: the user's prompt plus the tool sequence it took.
When several episodes look alike — similar wording AND a similar tool sequence —
that recurrence is surfaced once as a `RecurringPattern`. The engine then nudges
(the user, and optionally the model via skill_manage) to capture it as a
SKILL.md. The skills system itself is the source of truth; this module only
*detects* — it never stores skills of its own.

No network, no model call, no thread — detection is a cheap token/Jaccard pass
over a capped, persisted ring of recent episodes.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .config import CONFIG_DIR

# Articles/pronouns/prepositions carry no signal about *which* workflow a prompt
# describes. Verbs like "add"/"fix"/"run" are kept — they're often the most
# discriminating token in a coding request.
_STOPWORDS = {
    "the", "a", "an", "to", "and", "of", "in", "on", "for", "with", "please",
    "can", "could", "would", "you", "your", "i", "it", "this", "that", "these",
    "those", "my", "me", "is", "are", "was", "were", "be", "do", "does", "then",
    "from", "at", "as", "by", "we", "us", "so", "if", "or", "but", "into",
}


def _tokens(text: str) -> set[str]:
    return {
        w
        for w in re.findall(r"[a-z0-9_]+", (text or "").lower())
        if len(w) > 2 and w not in _STOPWORDS
    }


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _collapse(tools: list[str]) -> tuple[str, ...]:
    """Drop consecutive duplicates so 'read read read edit' reads as
    'read -> edit' — the shape of the workflow, not its repetition count."""
    out: list[str] = []
    for t in tools:
        if not out or out[-1] != t:
            out.append(t)
    return tuple(out)


@dataclass
class Episode:
    prompt: str
    tools: list[str] = field(default_factory=list)
    cwd: str = ""
    ts: float = 0.0


@dataclass
class RecurringPattern:
    signature: str
    suggested_name: str
    trigger_terms: list[str]
    tool_sequence: list[str]
    sample_prompts: list[str]
    count: int


class WorkflowDetector:
    # A pair of episodes is "the same workflow" when their prompts overlap at
    # least this much AND their tool sets overlap at least this much.
    PROMPT_SIM = 0.45
    TOOL_SIM = 0.5
    # How many lookalike episodes (including the triggering one) before we
    # surface the recurrence.
    MIN_OCCURRENCES = 3
    # A workflow is at least this many tool calls — single-tool Q&A isn't one.
    MIN_TOOLS = 2
    MAX_EPISODES = 300

    def __init__(self, path: Path | None = None):
        self.path = path or (CONFIG_DIR / "workflows.json")
        self.episodes: list[Episode] = []
        # Signatures we've already surfaced, so we nudge once, not every turn.
        self.suggested: list[str] = []
        self._load()

    # -- persistence -------------------------------------------------------
    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text())
        except (json.JSONDecodeError, OSError):
            return
        self.episodes = [Episode(**e) for e in data.get("episodes", []) if isinstance(e, dict)]
        self.suggested = list(data.get("suggested", []))

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "episodes": [asdict(e) for e in self.episodes],
            "suggested": self.suggested,
        }
        self.path.write_text(json.dumps(payload, indent=2))

    # -- recording / detection --------------------------------------------
    def record(self, prompt: str, tools: list[str], cwd: str, ts: float) -> RecurringPattern | None:
        """Log a completed submission. Return a RecurringPattern the first time
        a workflow crosses the recurrence threshold; otherwise None."""
        if len(tools) < self.MIN_TOOLS:
            return None
        ep = Episode(prompt=prompt, tools=list(tools), cwd=cwd, ts=ts)
        self.episodes.append(ep)
        if len(self.episodes) > self.MAX_EPISODES:
            self.episodes = self.episodes[-self.MAX_EPISODES:]
        pattern = self._detect(ep)
        self.save()
        return pattern

    def _detect(self, ep: Episode) -> RecurringPattern | None:
        ep_tok = _tokens(ep.prompt)
        ep_tools = set(ep.tools)
        if not ep_tok:
            return None
        cluster = [
            e
            for e in self.episodes
            if _jaccard(_tokens(e.prompt), ep_tok) >= self.PROMPT_SIM
            and _jaccard(set(e.tools), ep_tools) >= self.TOOL_SIM
        ]
        if len(cluster) < self.MIN_OCCURRENCES:
            return None

        tok_counts: Counter = Counter()
        for e in cluster:
            tok_counts.update(_tokens(e.prompt))
        trigger = [w for w, _ in tok_counts.most_common(6)]
        signature = "+".join(sorted(trigger[:4]))
        if signature in self.suggested:  # already surfaced once
            return None
        self.suggested.append(signature)

        seq_counts = Counter(_collapse(e.tools) for e in cluster)
        sequence = list(seq_counts.most_common(1)[0][0])
        return RecurringPattern(
            signature=signature,
            suggested_name="-".join(trigger[:3]) or "-".join(sequence[:3]) or "workflow",
            trigger_terms=trigger,
            tool_sequence=sequence,
            sample_prompts=[e.prompt for e in cluster[-3:]],
            count=len(cluster),
        )

    def reset(self) -> None:
        self.episodes = []
        self.suggested = []
        self.save()
