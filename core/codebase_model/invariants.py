"""The invariant compiler — the defensible core (see 03-invariant-compiler.md).

A prescriptive belief is only as trustworthy as it is *checkable*. This module
takes an invariant's ``:check`` s-expr and compiles it down to a **deterministic
graph query** that runs over the substrate edges and returns counterexamples
with real ``path:line`` locations — not an opinion the LLM eyeballs. The fraction
of invariants we can compile here is exactly the fraction we can trust without a
model in the loop.

Two vocabularies live side by side:

  * **compiled checks** — run over the whole graph (or a diff) for an invariant:
    ``(forbidden-edge :kind <edgekind> :from <pat> :to <pat>)`` and
    ``(no-global-construction :class <Name>)``.
  * **decision detectors** — run over a *proposed diff* so a rejected pattern is
    caught as it's reintroduced: ``(construct <Name>)`` and
    ``(call-name <name> :from <pat>)``.

Anything whose head we don't recognise (or that won't parse) is *uncompilable*
and stays soft — :meth:`InvariantEngine.compile_check` returns ``None`` and
:meth:`check_all` skips it. Enforcement is **asymmetric**: a violation only
hard-blocks when its invariant ``may_hard_block()`` (compiled-and-checked /
human-confirmed); everything else is SOFT, warn-only.

Depends only on :mod:`.store`, :mod:`.sexpr` and :mod:`.model`.
"""
from __future__ import annotations

import fnmatch
import re
import time
from typing import Callable

from . import sexpr
from .model import (Decision, Edge, EdgeKind, Enforcement, Invariant, NodeKind,
                    Violation, module_of)
from .sexpr import Symbol

# Heads we know how to compile, split by where they run.
_CHECK_HEADS = ("forbidden-edge", "no-global-construction")
_DETECTOR_HEADS = ("construct", "call-name")

# An edge-level match: the offending edge plus a human-readable detail string.
_Match = tuple[Edge, str]
_Matcher = Callable[[list[Edge]], list[_Match]]


def slugify(text: str) -> str:
    """Canonical id slug for a claim/reason (kebab-case, like core/skills.py)."""
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").strip().lower())
    return s.strip("-") or "invariant"


# --------------------------------------------------------------------------
# pattern matching — fnmatch glob OR bare path/module prefix
# --------------------------------------------------------------------------
def _pat(pat: str, value: str | None) -> bool:
    """``<pat>`` matches ``value`` if it globs it (``*``) or is a path/module
    prefix of it (``plugins`` matches ``plugins/foo.py`` and module ``plugins``)."""
    if value is None:
        return False
    if fnmatch.fnmatchcase(value, pat):
        return True
    if value == pat:
        return True
    p = pat.rstrip("/")
    return bool(p) and value.startswith(p + "/")


def _pat_any(pat: str, *values: str | None) -> bool:
    return any(_pat(pat, v) for v in values)


def _as_str(form: object) -> str:
    """Stringify an s-expr atom for use as a name/pattern (Symbol -> its name)."""
    if isinstance(form, Symbol):
        return form.name
    if isinstance(form, str):
        return form
    return str(form)


def _parse_form(form: list) -> tuple[str, list[str], dict[str, str]]:
    """Split a compiled-check form into ``(head, positionals, kwargs)``. Keyword
    args are ``:key value`` pairs; everything else is positional. Values are
    stringified so ``:class Renderer`` and ``:class "Renderer"`` both yield
    ``Renderer``."""
    head = _as_str(form[0])
    pos: list[str] = []
    kw: dict[str, str] = {}
    i = 1
    while i < len(form):
        tok = form[i]
        if isinstance(tok, Symbol) and tok.name.startswith(":"):
            key = tok.name[1:]
            kw[key] = _as_str(form[i + 1]) if i + 1 < len(form) else ""
            i += 2
        else:
            pos.append(_as_str(tok))
            i += 1
    return head, pos, kw


class InvariantEngine:
    """Compiles + evaluates prescriptive checks and decision detectors against
    the substrate graph held in ``store``."""

    def __init__(self, store):
        self.store = store

    # ------------------------------------------------------------------
    # capture
    # ------------------------------------------------------------------
    def add(self, claim: str, *, check: str = "",
            enforcement: str = Enforcement.SOFT, confidence: object = 0.5,
            source: str = "inferred", inv_id: str | None = None) -> Invariant:
        """Record a prescriptive invariant. Inferred candidates default to SOFT
        enforcement; ``check`` makes it compilable (and so eligible for HARD)."""
        inv = Invariant(
            id=inv_id or slugify(claim), claim=claim, check=check,
            enforcement=enforcement, confidence=confidence, source=source,
            created=time.time())
        self.store.put_invariant(inv)
        return inv

    def add_decision(self, *, reason: str, detector: str = "",
                     accepted_pattern: str = "", status: str = "rejected",
                     dec_id: str | None = None) -> Decision:
        """Record a decision (typically a rejected pattern). ``reason`` is the
        durable intent that survives the code being refactored away."""
        dec = Decision(
            id=dec_id or slugify(reason), status=status, detector=detector,
            accepted_pattern=accepted_pattern, reason=reason, created=time.time())
        self.store.put_decision(dec)
        return dec

    # ------------------------------------------------------------------
    # compilation
    # ------------------------------------------------------------------
    def _src_idents(self, src: str) -> tuple[str, str, str]:
        """``(module, name, path)`` for an edge's src node — via the store when
        the node is known, else parsed straight from the ``path::qualname`` id so
        the matcher stays usable for unresolved srcs too."""
        node = self.store.get_node(src)
        if node is not None:
            return node.module, node.name, node.path
        path, _, qual = src.partition("::")
        module = module_of(path)
        return module, (qual or module or path), path

    def _is_module_src(self, src: str) -> bool:
        """True iff the edge originates at module/global scope (its src node is a
        MODULE). Module-scope edges carry the bare module id (no ``::``)."""
        node = self.store.get_node(src)
        if node is not None:
            return node.kind == NodeKind.MODULE
        return "::" not in src

    def _build_matcher(self, head: str, pos: list[str], kw: dict[str, str]) -> _Matcher | None:
        """Turn a parsed form into a closure that yields offending edges. Returns
        ``None`` if the form is malformed for its head."""
        if head == "forbidden-edge":
            kind = kw.get("kind", "")
            frm, to = kw.get("from", "*"), kw.get("to", "*")

            def match_forbidden(edges: list[Edge]) -> list[_Match]:
                out: list[_Match] = []
                for e in edges:
                    if kind and e.kind != kind:
                        continue
                    s_mod, s_name, s_path = self._src_idents(e.src)
                    if not _pat_any(frm, s_mod, s_name, s_path):
                        continue
                    if not _pat(to, e.dst):
                        continue
                    out.append((e, f"{s_name or s_path} -> {e.dst}"))
                return out
            return match_forbidden

        if head in ("no-global-construction", "construct"):
            # :class kwarg (invariant form) or a single positional (detector form)
            name = kw.get("class") or (pos[0] if pos else "")
            if not name:
                return None

            def match_global(edges: list[Edge]) -> list[_Match]:
                out: list[_Match] = []
                for e in edges:
                    if e.kind != EdgeKind.CALLS or e.dst != name:
                        continue
                    if not self._is_module_src(e.src):
                        continue
                    out.append((e, f"global construction of {name}"))
                return out
            return match_global

        if head == "call-name":
            name = pos[0] if pos else kw.get("name", "")
            if not name:
                return None
            frm = kw.get("from", "*")

            def match_call(edges: list[Edge]) -> list[_Match]:
                out: list[_Match] = []
                for e in edges:
                    if e.kind != EdgeKind.CALLS or e.dst != name:
                        continue
                    s_mod, s_name, s_path = self._src_idents(e.src)
                    if not _pat_any(frm, s_mod, s_name, s_path):
                        continue
                    out.append((e, f"{s_name or s_path} -> {name}"))
                return out
            return match_call

        return None

    def _compile(self, check: str, allowed: tuple[str, ...]) -> _Matcher | None:
        """Parse ``check`` and build a matcher iff its head is in ``allowed``."""
        if not check or not check.strip():
            return None
        form = sexpr.try_read(check)
        if not isinstance(form, list) or not form:
            return None
        head, pos, kw = _parse_form(form)
        if head not in allowed:
            return None
        return self._build_matcher(head, pos, kw)

    def compile_check(self, check_sexpr: str) -> Callable[[list[Edge]], list[Violation]] | None:
        """Compile a check s-expr into an evaluator over a list of edges, or
        ``None`` if the head is unknown / the form won't parse. The evaluator's
        violations carry the offending edge's own ``path:line`` (deterministic);
        :meth:`run` re-resolves the location against the store and attaches the
        owning invariant's id/claim/enforcement."""
        matcher = self._compile(check_sexpr, _CHECK_HEADS)
        if matcher is None:
            return None

        def evaluator(edges: list[Edge]) -> list[Violation]:
            return [Violation(invariant_id="", claim=check_sexpr,
                              location=f"{e.path}:{e.lineno}", detail=detail)
                    for e, detail in matcher(edges)]
        return evaluator

    # ------------------------------------------------------------------
    # evaluation
    # ------------------------------------------------------------------
    def run(self, inv: Invariant, *, edges: list[Edge] | None = None) -> list[Violation]:
        """Evaluate ``inv``'s compiled check, returning a Violation per offending
        edge. Location is ``src_node.path:edge.lineno`` (looked up in the store).
        A violation only hard-blocks when ``inv.may_hard_block()``; otherwise it
        is SOFT, however ``inv.enforcement`` is set."""
        matcher = self._compile(inv.check, _CHECK_HEADS)
        if matcher is None:
            return []
        if edges is None:
            edges = self.store.all_edges()
        enforcement = Enforcement.HARD if inv.may_hard_block() else Enforcement.SOFT
        out: list[Violation] = []
        for e, detail in matcher(edges):
            node = self.store.get_node(e.src)
            path = node.path if node is not None else e.path
            out.append(Violation(
                invariant_id=inv.id, claim=inv.claim,
                location=f"{path}:{e.lineno}", detail=detail,
                enforcement=enforcement))
        return out

    def _run_and_record(self, edges: list[Edge] | None, commit: str) -> list[Violation]:
        """Run every *compiled* invariant, record a pass/fail result keyed by
        commit, and persist the latest counterexample. Uncompiled (soft,
        semantic) invariants are skipped — there's nothing deterministic to run."""
        out: list[Violation] = []
        run_at = time.time()
        for inv in self.store.all_invariants():
            if not inv.compiled:
                continue
            vios = self.run(inv, edges=edges)
            passed = not vios
            counterexample = "" if passed else vios[0].location
            self.store.record_invariant_result(inv.id, passed, counterexample, run_at, commit)
            inv.counterexample = counterexample
            self.store.put_invariant(inv)
            out.extend(vios)
        return out

    def check_all(self, commit: str = "") -> list[Violation]:
        """Run every compiled invariant over the whole graph."""
        return self._run_and_record(None, commit)

    def check_diff(self, changed_files, commit: str = "") -> list[Violation]:
        """Run every compiled invariant, but only over edges originating in
        ``changed_files`` — the cheap per-diff check."""
        changed = set(changed_files)
        edges = [e for e in self.store.all_edges() if e.path in changed]
        return self._run_and_record(edges, commit)

    def detect_rejected(self, changed_files) -> list[Violation]:
        """Run each non-archived decision's detector over edges in the diff. A
        match means the agent is reintroducing a rejected pattern: surfaced as a
        SOFT Violation carrying the decision's id and durable reason."""
        changed = set(changed_files)
        edges = [e for e in self.store.all_edges() if e.path in changed]
        out: list[Violation] = []
        for dec in self.store.all_decisions():
            matcher = self._compile(dec.detector, _DETECTOR_HEADS)
            if matcher is None:
                continue
            for e, detail in matcher(edges):
                node = self.store.get_node(e.src)
                path = node.path if node is not None else e.path
                out.append(Violation(
                    invariant_id=dec.id, claim=dec.reason,
                    location=f"{path}:{e.lineno}", detail=detail,
                    enforcement=Enforcement.SOFT))
        return out
