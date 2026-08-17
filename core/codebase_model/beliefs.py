"""Descriptive-belief lifecycle — the cache that summarizes the code.

A :class:`~.model.Belief` is *defeasible*: it summarizes the substrate, and on
conflict with the code it loses (``:demote``). This module owns that policy plus
the memory-bounding mechanics from ``docs/codebase-mental-model/05-memory-bounding.md``:

  * **Supersession, not accumulation** — re-stating a belief raises its confidence
    *in place* and bumps its confirmation count; it never appends a new row and
    keeps the original ``created`` provenance.
  * **Lazy re-verification == drift handling == eviction.** A diff marks citing
    beliefs stale cheaply (no LLM, :meth:`invalidate`). The expensive re-fetch +
    verify (:meth:`reverify`) fires *only* when a stale belief is actually
    consulted. A stale belief no one reads again is simply evicted for free.
  * **LRU cap.** Beliefs are a cache; under pressure the least-recently-consulted
    (lowest-confidence as tiebreaker) are dropped, bounded by ``max_beliefs``.

Drift and eviction are the *same* mechanism: :meth:`demote` lowers confidence and,
once it falls below the floor, deletes the belief outright.
"""
from __future__ import annotations

import hashlib
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import citations
from .citations import RefetchResult
from .model import Belief, BeliefKind, DEFAULT_ON_CONFLICT
from .store import ModelStore

# Below this confidence a descriptive belief is no longer worth its row: demote
# unifies "the code drifted away from this claim" with eviction (05-memory-bounding).
_CONFIDENCE_FLOOR = 0.15

Verifier = Callable[[Belief, "list[RefetchResult]"], bool]


def slugify(text: str) -> str:
    """Canonical kebab id for a belief from its claim (mirrors core/skills.py)."""
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").strip().lower())
    return s.strip("-") or "belief"


def belief_id_for(claim: str) -> str:
    """Stable belief id: readable slug prefix + hash of the normalized claim.

    slugify alone collides — "uses X." and "uses X?" both slugify to
    "uses-x", so two differently-worded claims would silently supersede each
    other. Hashing the whitespace-normalized claim keeps identical
    restatements mapping to the same row (supersession still works) while
    distinct wordings get distinct ids.
    """
    normalized = re.sub(r"\s+", " ", (claim or "").strip().lower())
    digest = hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:8]
    return f"{slugify(claim)[:48]}-{digest}"


def _iso(ts: float) -> str:
    """UTC ISO date stamp for ``verified`` (date granularity is enough)."""
    return datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat()


def _clamp(c: float) -> float:
    return 0.0 if c < 0.0 else 1.0 if c > 1.0 else c


class BeliefManager:
    """Owns descriptive beliefs: creation/supersession, consultation (LRU),
    reinforcement, demotion, invalidation, and lazy re-verification."""

    def __init__(self, store: ModelStore, *, max_beliefs: int = 500,
                 verifier: Verifier | None = None, clock: Callable[[], float] = time.time):
        self.store = store
        self.max_beliefs = max_beliefs
        self.verifier = verifier
        self.clock = clock

    # -- creation / supersession ------------------------------------------
    def add(self, claim: str, *, confidence: float = 0.7,
            justified_by: list[str] | None = None, source: str = "inferred",
            belief_id: str | None = None,
            kind: str = BeliefKind.DESCRIPTIVE) -> Belief:
        """Record (or supersede) a belief. If the id already exists the belief is
        *replaced* inheriting its provenance: confirmations grow by one, the
        original ``created`` is kept, and confidence is raised toward
        ``max(old, new)`` (a re-statement never *lowers* it). See 05's
        "supersession, not accumulation"."""
        now = self.clock()
        bid = belief_id or belief_id_for(claim)
        justified_by = list(justified_by or [])

        old = self.store.get_belief(bid)
        if old is not None:
            # supersede in place: keep older provenance, raise confidence toward
            # the better of the two, accumulate a confirmation.
            target = max(old.confidence, confidence)
            new_conf = _clamp(old.confidence + (target - old.confidence) * 0.5)
            confirmations = old.confirmations + 1
            created = old.created
            # a re-statement with no fresh citations keeps the prior evidence
            if not justified_by:
                justified_by = list(old.justified_by)
        else:
            new_conf = _clamp(confidence)
            confirmations = 1
            created = now

        belief = Belief(
            id=bid, claim=claim, confidence=new_conf, justified_by=justified_by,
            kind=kind, on_conflict=DEFAULT_ON_CONFLICT.get(kind, "demote"),
            verified=_iso(now), confirmations=confirmations,
            last_consulted=now,  # a just-learned belief is in use; protect from LRU
            stale=False, created=created, source=source)
        self.store.put_belief(belief)
        self._evict()
        return belief

    def get(self, belief_id: str) -> Belief | None:
        return self.store.get_belief(belief_id)

    def all(self) -> list[Belief]:
        return self.store.all_beliefs()

    # -- consultation ------------------------------------------------------
    def consult(self, belief_id: str) -> Belief | None:
        """Touch a belief (LRU) and return it. Kept deliberately cheap — the
        service decides when a *stale* consulted belief warrants re-verification."""
        belief = self.store.get_belief(belief_id)
        if belief is None:
            return None
        now = self.clock()
        self.store.touch_belief(belief_id, now)
        belief.touch(now)
        return belief

    # -- confidence movement ----------------------------------------------
    def reinforce(self, belief_id: str) -> Belief | None:
        """Confirmation: raise confidence toward 1 in place, bump confirmations,
        clear stale, restamp ``verified``."""
        belief = self.store.get_belief(belief_id)
        if belief is None:
            return None
        belief.confidence = _clamp(belief.confidence + (1.0 - belief.confidence) * 0.5)
        belief.confirmations += 1
        belief.stale = False
        belief.verified = _iso(self.clock())
        self.store.put_belief(belief)
        return belief

    def demote(self, belief_id: str, amount: float = 0.25) -> Belief | None:
        """The descriptive ``:demote`` policy — code won, so lower confidence.
        Below the floor the belief is deleted (drift and eviction unified)."""
        belief = self.store.get_belief(belief_id)
        if belief is None:
            return None
        belief.confidence = _clamp(belief.confidence - amount)
        if belief.confidence < _CONFIDENCE_FLOOR:
            self.store.delete_belief(belief_id)
            return None
        self.store.put_belief(belief)
        return belief

    # -- invalidation (cheap, on diff) ------------------------------------
    def invalidate(self, changed_files: list[str]) -> list[str]:
        """Mark every belief citing a changed file stale — a content-addressed
        match, no LLM. Returns the affected belief ids."""
        ids: list[str] = []
        seen: set[str] = set()
        for path in changed_files:
            for bid in self.store.beliefs_citing_file(path):
                if bid not in seen:
                    seen.add(bid)
                    ids.append(bid)
        self.store.mark_beliefs_stale(ids)
        return ids

    def invalidate_symbols(self, path: str, changed_symbols: set[str]) -> list[str]:
        """Symbol-granular invalidation: stale-mark only the beliefs citing
        ``path`` whose cited symbol actually changed (by slice hash). Beliefs
        citing the whole file (no symbol) go stale on any change — there's
        nothing narrower to compare. Returns the affected belief ids."""
        tails = {s.split(".")[-1] for s in changed_symbols}
        ids: list[str] = []
        for bid, symbol in self.store.beliefs_citing(path):
            hit = (
                not symbol
                or symbol in changed_symbols
                or symbol.split(".")[-1] in tails  # cited "refund" vs node "RefundService.refund"
            )
            if hit and bid not in ids:
                ids.append(bid)
        self.store.mark_beliefs_stale(ids)
        return ids

    # -- lazy re-verification (expensive, on consult) ---------------------
    def reverify(self, belief_id: str, repo_root: Path | str) -> bool:
        """Re-fetch each cited slice and decide the belief's fate. This is the
        *only* place the expensive verification runs, and only when a stale belief
        is actually wanted:

          * any citation that no longer resolves ⇒ ``demote`` (the evidence is
            gone, the code moved on) and return False;
          * otherwise run the verifier (or trust on refetch-found when none given)
            ⇒ ``reinforce`` on True, ``demote`` on False.
        """
        belief = self.store.get_belief(belief_id)
        if belief is None:
            return False
        results: list[RefetchResult] = []
        for raw in belief.justified_by:
            res = citations.refetch(citations.parse(raw), repo_root)
            results.append(res)
            if not res.found:
                # a citation vanished: the substrate drifted out from under us
                self.demote(belief_id)
                return False
        ok = self.verifier(belief, results) if self.verifier else True
        if ok:
            self.reinforce(belief_id)
            return True
        self.demote(belief_id)
        return False

    # -- memory bounding ---------------------------------------------------
    def _evict(self) -> int:
        """Enforce the LRU cap: while over ``max_beliefs``, drop the
        least-recently-consulted (lowest-confidence tiebreak) beliefs. Returns the
        number evicted. Beliefs are a cache — eviction costs only a re-inference."""
        evicted = 0
        while True:
            count = self.store.count_beliefs()
            if count <= self.max_beliefs:
                break
            victims = self.store.beliefs_by_lru(count - self.max_beliefs)
            if not victims:
                break
            for v in victims:
                self.store.delete_belief(v.id)
                evicted += 1
        return evicted
