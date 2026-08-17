"""The query surface — the *read* side the agent actually talks to.

Everything below is a thin projection over already-built machinery: the indexer's
reverse-reachability for impact, the belief cache for ownership/recall, and the
invariant engine for "is this allowed". The point of this module is to turn those
into the three task-shaped questions from the acceptance harness (see
``docs/codebase-mental-model/11-acceptance-test.md``) — *affects*, *owns*,
*allowed* — plus the small "load-first" architecture summary, and to keep the
projection in the s-expression idiom (``08-representation.md``) so what the agent
reads round-trips with what it stores.

Read-only: nothing here mutates the substrate. The one side effect is the LRU
``touch`` of a consulted belief (a belief a task leans on is load-bearing and must
survive eviction) — done via ``BeliefManager.consult``.

Depends on :mod:`.store`, :mod:`.indexer`, :mod:`.beliefs`, :mod:`.invariants`
and :mod:`.sexpr`.
"""
from __future__ import annotations

import re

from . import sexpr
from .model import BeliefKind, Violation
from .sexpr import Symbol

# Words that carry no subject (question scaffolding + the routing triggers
# themselves) — stripped before guessing what a free-text question is *about*.
_STOP = {
    "what", "which", "who", "whom", "whose", "where", "when", "why", "how",
    "does", "do", "did", "is", "are", "was", "were", "be", "the", "a", "an",
    "to", "of", "in", "on", "for", "and", "or", "me", "my", "give", "show",
    "tell", "here", "this", "that", "it", "if", "i", "you", "we", "get",
    "affect", "affects", "affected", "affecting", "impact", "impacts",
    "change", "changes", "changed", "changing", "own", "owns", "owned",
    "owner", "ownership", "responsible", "responsibility", "allow", "allowed",
    "allows", "pattern", "should", "can", "constructing", "construct",
    "singleton", "architecture", "module", "modules",
}

# A dotted or CamelCase token is almost certainly the symbol the user means.
_SYMBOLISH = re.compile(r"[A-Za-z_][\w]*\.[\w.]+|[A-Z][A-Za-z0-9]*[A-Z][\w]*|[A-Z][a-z0-9]+")


def _tokens(text: str) -> set[str]:
    """Lowercased alphanumeric tokens of ``text`` (for fuzzy claim/topic match)."""
    return {t for t in re.split(r"[^a-z0-9]+", (text or "").lower()) if len(t) > 1}


def _subject(question: str) -> str:
    """Best-effort: pull the symbol/topic a free-text question is *about*. Prefer a
    dotted/CamelCase identifier; else the longest non-stopword bare word."""
    m = _SYMBOLISH.search(question or "")
    if m:
        return m.group(0)
    words = [w for w in re.findall(r"[A-Za-z_][\w]*", question or "")
             if w.lower() not in _STOP]
    return max(words, key=len) if words else (question or "").strip()


def _vio_dict(v: Violation) -> dict:
    """Flatten a Violation for the JSON-ish query result."""
    return {"invariant_id": v.invariant_id, "claim": v.claim,
            "location": v.location, "detail": v.detail,
            "enforcement": v.enforcement, "blocking": v.blocking}


class QuerySurface:
    """The agent-facing read API over the model: impact, ownership, allowance,
    plus the load-first architecture index and per-node fact projection."""

    def __init__(self, store, indexer, beliefs, invariants):
        self.store = store
        self.indexer = indexer
        self.beliefs = beliefs
        self.invariants = invariants

    # -- affects (blast radius) -------------------------------------------
    def affects(self, target: str) -> dict:
        """What an edit to ``target`` can reach, off the stored call graph. ``target``
        is a symbol name / node id, or a ``*.py`` path (then the union over every
        symbol it defines). Returns affected symbol ids, their files, and a count."""
        target = str(target)
        file_nodes = self.store.nodes_in_file(target)
        if file_nodes:
            symbols: list[str] = []
            seen: set[str] = set()
            for node in file_nodes:
                for sid in self.indexer.blast_radius(node.id):
                    if sid not in seen:
                        seen.add(sid)
                        symbols.append(sid)
            files = self.indexer.affected_files(target)
        else:
            symbols = self.indexer.blast_radius(target)
            paths: set[str] = set()
            for sid in symbols:
                node = self.store.get_node(sid)
                if node is not None and node.path:
                    paths.add(node.path)
            files = sorted(paths)
        return {"target": target, "affected_symbols": symbols,
                "affected_files": files, "count": len(symbols)}

    # -- owns (descriptive recall) ----------------------------------------
    def owns(self, topic: str) -> dict:
        """Recall descriptive ownership beliefs about ``topic``. Each hit is
        ``consult``-ed (LRU touch — recall is what keeps a belief alive). Returns the
        matching beliefs with their citations + staleness, and a prose answer."""
        topic = str(topic)
        topic_tokens = _tokens(topic)
        hits: list[dict] = []
        for belief in self.beliefs.all():
            if belief.kind != BeliefKind.DESCRIPTIVE:
                continue
            claim_l = belief.claim.lower()
            if "own" not in claim_l:
                continue
            # the belief must actually concern the topic, not merely say "owns"
            if topic_tokens and not (topic_tokens & _tokens(belief.claim)):
                continue
            fresh = self.beliefs.consult(belief.id) or belief
            hits.append({"id": fresh.id, "claim": fresh.claim,
                         "confidence": fresh.confidence,
                         "citations": list(fresh.justified_by),
                         "stale": fresh.stale})
        if hits:
            parts = [f"{h['claim']} (confidence {h['confidence']:.2f}"
                     f"{', stale' if h['stale'] else ''})" for h in hits]
            answer = f"{topic}: " + "; ".join(parts)
        else:
            answer = f"No retained belief about who owns {topic!r}."
        return {"topic": topic, "beliefs": hits, "answer": answer}

    # -- allowed (prescriptive + rejected patterns) -----------------------
    def allowed(self, description: str, changed_files: list[str] | None = None) -> dict:
        """Is the change described by ``description`` permitted? Runs the compiled
        invariants (over the diff when ``changed_files`` is given, else the whole
        graph) and the rejected-pattern detectors, then surfaces the violations whose
        claim/reason overlaps ``description`` (falling back to all). Blocked iff any
        surfaced violation is HARD (``may_hard_block``)."""
        if changed_files:
            vios = self.invariants.check_diff(changed_files)
            detect_over = changed_files
        else:
            vios = self.invariants.check_all()
            detect_over = self.store.all_files()
        vios = list(vios) + list(self.invariants.detect_rejected(detect_over))

        desc_tokens = _tokens(description)
        relevant = [v for v in vios
                    if desc_tokens & _tokens(f"{v.claim} {v.detail}")]
        surfaced = relevant or vios
        blocking = any(v.blocking for v in surfaced)
        return {"allowed": not blocking,
                "violations": [_vio_dict(v) for v in surfaced],
                "blocking": blocking}

    # -- architecture index (load first) ----------------------------------
    def architecture_index(self) -> str:
        """The small summary an agent loads before anything else: every module with
        its node count, the belief count, and the compiled/confirmed invariants as
        their check s-exprs. Kept under ~2KB by capping the invariant list."""
        lines = ["(architecture"]
        mod_parts: list[str] = []
        for module in self.store.all_modules():
            n = len(self.store.nodes_in_module(module))
            mod_parts.append(f"({module} {n})")
        lines.append("  (modules " + " ".join(mod_parts) + ")")
        lines.append(f"  (beliefs {self.store.count_beliefs()})")

        inv_lines: list[str] = []
        for inv in self.store.all_invariants():
            if not (inv.compiled or inv.confirmed):
                continue
            conf = "confirmed" if inv.confirmed else f"{inv.confidence}"
            check = inv.check.strip() or "nil"
            inv_lines.append(
                f"    (invariant {inv.id} :check {check} "
                f":enforcement {inv.enforcement} :confidence {conf})")
        if inv_lines:
            lines.append("  (invariants")
            lines.extend(inv_lines)
            lines.append("  )")
        else:
            lines.append("  (invariants)")
        lines.append(")")

        out = "\n".join(lines)
        if len(out) > 2048:  # hard cap: drop trailing invariants until it fits
            while inv_lines and len(out) > 2048:
                inv_lines.pop()
                body = ["(architecture",
                        "  (modules " + " ".join(mod_parts) + ")",
                        f"  (beliefs {self.store.count_beliefs()})"]
                if inv_lines:
                    body.append("  (invariants")
                    body.extend(inv_lines)
                    body.append("  )")
                else:
                    body.append("  (invariants)")
                body.append(")")
                out = "\n".join(body)
        return out

    # -- per-node fact projection -----------------------------------------
    def project_facts(self, node_id: str) -> str:
        """Project a node's out-edges as derived facts:
        ``(fact (<kind> <src> <dst>) :at <commit>)``, one per line, via
        :func:`sexpr.write` so they round-trip the reader."""
        node = self.store.get_node(node_id)
        src_name = node.name if node is not None else node_id
        commit = self.store.get_meta("commit") or ""
        out: list[str] = []
        for e in self.store.edges_from(node_id):
            relation = [Symbol(e.kind), Symbol(src_name or node_id), Symbol(e.dst)]
            form = [Symbol("fact"), relation, Symbol(":at"), commit]
            out.append(sexpr.write(form))
        return "\n".join(out)

    # -- free-text routing ------------------------------------------------
    def answer(self, question: str) -> str:
        """Route a natural-language question to the right surface and render a
        human-readable answer. Keyword-routed: affect/impact/change ⇒ affects;
        own/responsib ⇒ owns; allow/can-i/pattern/should ⇒ allowed; else the
        architecture index."""
        q = (question or "").lower()
        if any(k in q for k in ("affect", "impact", "change")):
            res = self.affects(_subject(question))
            files = ", ".join(res["affected_files"]) or "no other files"
            return (f"Changing {res['target']} affects {res['count']} symbol(s) "
                    f"across: {files}.")
        if "own" in q or "responsib" in q:
            return self.owns(_subject(question))["answer"]
        if any(k in q for k in ("allow", "can i", "pattern", "should")):
            res = self.allowed(question)
            if res["allowed"]:
                return "Allowed: no blocking invariant or rejected pattern matched."
            details = "; ".join(
                f"{v['claim']} at {v['location']}" for v in res["violations"]
                if v["blocking"]) or "a rejected pattern"
            return f"Not allowed — {details}."
        return self.architecture_index()
