"""Core data model — the shared contract every other module conforms to.

Two families of thing live here:

  * **Substrate** (:class:`Node`, :class:`Edge`) — regenerable ground truth
    produced by the parser. No confidence. A materialized view of HEAD.
  * **Inferred** (:class:`Belief`, :class:`Invariant`, :class:`Decision`) — the
    LLM-produced cache. Carries confidence and, crucially, an ``on_conflict``
    policy that differs by kind. See ``docs/codebase-mental-model/02-belief-types.md``.

These are plain dataclasses; persistence (SQLite rows) and projection
(s-expressions) are handled by :mod:`.store` and :mod:`.sexpr` respectively, so
the model itself stays dependency-free.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import PurePosixPath


# --------------------------------------------------------------------------
# Substrate vocabulary
# --------------------------------------------------------------------------
class NodeKind:
    MODULE = "module"
    CLASS = "class"
    FUNCTION = "function"
    METHOD = "method"
    VARIABLE = "variable"


class EdgeKind:
    CALLS = "calls"
    IMPORTS = "imports"
    CONTAINS = "contains"
    INHERITS = "inherits"
    REFERENCES = "references"
    WRITES = "writes"  # writes to a persistence/IO sink (data-flow pillar, best-effort)


# --------------------------------------------------------------------------
# Inferred-layer vocabulary
# --------------------------------------------------------------------------
class BeliefKind:
    """The three belief types. Conflict resolution branches on this."""
    DERIVED = "derived"          # a fact mirrored from the substrate (rarely stored)
    DESCRIPTIVE = "descriptive"  # a claim *about* the system; defers to code
    PRESCRIPTIVE = "prescriptive"  # a rule the code must obey; judges code


class OnConflict:
    """What to do when a belief disagrees with the code. The whole point is that
    this is NOT uniform across belief kinds (see 02-belief-types.md)."""
    REFRESH = "refresh"  # derived: re-derive from the substrate
    DEMOTE = "demote"    # descriptive: lower confidence / evict; code wins
    RAISE = "raise"      # prescriptive: emit a violation — do NOT weaken the rule


class Enforcement:
    """How hard an invariant bites. Asymmetric: a false invariant (vetoes a
    correct edit) is worse than a missing one, so only compiled/confirmed
    invariants may hard-block."""
    HARD = "hard"  # may veto an edit; reserved for compiled-and-checked / human-confirmed
    SOFT = "soft"  # warn only; for inferred candidates


# The default on_conflict for each belief kind. A belief may override, but these
# encode the policy from the spec so callers don't have to remember it.
DEFAULT_ON_CONFLICT = {
    BeliefKind.DERIVED: OnConflict.REFRESH,
    BeliefKind.DESCRIPTIVE: OnConflict.DEMOTE,
    BeliefKind.PRESCRIPTIVE: OnConflict.RAISE,
}


# --------------------------------------------------------------------------
# Substrate dataclasses
# --------------------------------------------------------------------------
def node_id(path: str, qualname: str = "") -> str:
    """Stable, content-addressable symbol id: ``path::qualname`` (a bare module
    is just its path). Posix-normalised so ids are stable across platforms."""
    p = PurePosixPath(path).as_posix()
    return f"{p}::{qualname}" if qualname else p


def module_of(path: str) -> str:
    """The conceptual module a file belongs to — its top two path segments, or
    the top one for shallow trees. Generic (no per-repo config): ``billing`` for
    ``billing/refunds.py``, ``core/codebase_model`` for a deeper file."""
    parts = PurePosixPath(path).parts
    if len(parts) <= 1:
        return parts[0] if parts else ""
    if len(parts) == 2:
        return parts[0]
    return "/".join(parts[:2])


@dataclass
class Node:
    id: str
    kind: str
    name: str            # qualname, e.g. "RefundService.refund"
    path: str            # repo-relative posix path
    lineno: int = 0
    end_lineno: int = 0
    module: str = ""
    content_hash: str = ""  # hash of the *file* this node was parsed from
    slice_hash: str = ""    # hash of this node's own line span — the unit of
                            # symbol-granular belief invalidation

    def __post_init__(self) -> None:
        if not self.module:
            self.module = module_of(self.path)


@dataclass
class Edge:
    src: str             # node id
    dst: str             # node id (resolved) or a bare name (unresolved)
    kind: str
    path: str = ""       # file the edge originates in
    lineno: int = 0
    resolved: bool = False  # True iff dst is a known node id


# --------------------------------------------------------------------------
# Inferred-layer dataclasses
# --------------------------------------------------------------------------
@dataclass
class Belief:
    """A descriptive inferred belief. Defeasible; defers to code on conflict.

    ``confidence`` is a float in [0, 1]. ``justified_by`` holds content-addressed
    citations (``file @ symbol @ commit``) that make the belief re-derivable and
    drive invalidation. See 04-learned-not-decorative.md.
    """
    id: str
    claim: str                       # s-expr text, e.g. "(owns billing refunds)"
    confidence: float = 0.5
    justified_by: list[str] = field(default_factory=list)
    kind: str = BeliefKind.DESCRIPTIVE
    on_conflict: str = OnConflict.DEMOTE
    verified: str = ""               # ISO date of last verification
    confirmations: int = 1           # accumulated confirmation count (supersession)
    last_consulted: float = 0.0      # ts for LRU eviction
    stale: bool = False
    created: float = 0.0
    source: str = "inferred"         # inferred | pr | commit | doc | human

    def touch(self, ts: float) -> None:
        """Record a consultation (drives LRU; a belief read by a task is
        load-bearing and should survive eviction)."""
        self.last_consulted = ts


@dataclass
class Invariant:
    """A prescriptive belief. Challenges the code: on conflict it RAISES a
    violation rather than marking itself stale.

    ``check`` is an s-expr the compiler interprets against the graph (see
    :mod:`.invariants`); when absent the invariant is a soft, LLM-checked belief.
    ``confidence`` is either the symbol ``"confirmed"`` or a float for inferred
    candidates.
    """
    id: str
    claim: str
    check: str = ""                  # compiled-check s-expr; "" => soft / semantic
    enforcement: str = Enforcement.SOFT
    confidence: object = 0.5         # float | "confirmed"
    on_conflict: str = OnConflict.RAISE
    counterexample: str = ""         # location of last failure; "" => passing/unrun
    source: str = "inferred"
    created: float = 0.0

    @property
    def compiled(self) -> bool:
        return bool(self.check.strip())

    @property
    def confirmed(self) -> bool:
        return self.confidence == "confirmed"

    def may_hard_block(self) -> bool:
        """Asymmetric enforcement gate: only compiled-and-checked or
        human-confirmed invariants are allowed to veto an edit."""
        return self.enforcement == Enforcement.HARD and (self.compiled or self.confirmed)


@dataclass
class Decision:
    """A recorded decision, typically a *rejected pattern*. Its ``detector`` is
    run against a proposed diff so the pattern is caught as it's reintroduced.
    Durable core: never auto-evicted; archived (not deleted) when its module is
    gone. See 05-memory-bounding.md and 08-representation.md."""
    id: str
    status: str = "rejected"         # rejected | accepted
    detector: str = ""               # s-expr matched against a diff/AST
    accepted_pattern: str = ""       # the constructive alternative
    reason: str = ""                 # durable intent — survives refactors
    created: float = 0.0
    archived: bool = False


# --------------------------------------------------------------------------
# Query result types
# --------------------------------------------------------------------------
@dataclass
class Violation:
    """The output of a failed prescriptive check: a finding with a location, not
    a belief. See 03-invariant-compiler.md."""
    invariant_id: str
    claim: str
    location: str        # "path:line" of the counterexample
    detail: str = ""
    enforcement: str = Enforcement.SOFT

    @property
    def blocking(self) -> bool:
        return self.enforcement == Enforcement.HARD
