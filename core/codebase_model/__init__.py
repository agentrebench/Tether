"""Persistent Codebase Mental Model.

A persistent, queryable, incrementally-updated model of a repository that the
agent consults *before* retrieval — so it reasons over accumulated understanding
instead of rebuilding it from grep/embeddings every session.

Design spec lives in ``docs/codebase-mental-model/``. The two-layer split that
governs everything:

  * Derived substrate (this: nodes/edges from the AST) — regenerable ground
    truth, no confidence, ``O(Δ)`` to update. Lives in :mod:`.substrate` /
    :mod:`.indexer`, stored by :mod:`.store`.
  * Inferred layer (beliefs/invariants/decisions) — LLM-produced, defeasible,
    a lazy cache. Lives in :mod:`.beliefs` / :mod:`.invariants`.

Conflict resolution **branches on belief type** (see :mod:`.model`): derived
facts ``:refresh``, descriptive beliefs ``:demote`` (code wins), prescriptive
invariants ``:raise`` a violation (the inversion that is the point of the
system).

Deliberate adaptations from the spec, for a Python harness:
  * Storage is SQLite (stdlib ``sqlite3``) — the spec's single source of truth.
  * The "Lisp query surface" is implemented in Python (:mod:`.query`) over a
    real s-expression reader/printer/evaluator (:mod:`.sexpr`); compiled
    invariant checks and the agent-facing projection are genuine s-exprs.

Public entry point is :class:`.service.CodebaseModel`.
"""
from __future__ import annotations

from .model import (
    Belief,
    Decision,
    Edge,
    EdgeKind,
    Invariant,
    Node,
    NodeKind,
    OnConflict,
    BeliefKind,
    Enforcement,
    module_of,
    node_id,
)

__all__ = [
    "Belief",
    "Decision",
    "Edge",
    "EdgeKind",
    "Invariant",
    "Node",
    "NodeKind",
    "OnConflict",
    "BeliefKind",
    "Enforcement",
    "module_of",
    "node_id",
]
