"""The acceptance harness — the runnable green/red bar from doc 11.

This is the experiment that decides whether the model *learned* or merely
*decorated*. The procedure is deliberately brutal (see
``docs/codebase-mental-model/11-acceptance-test.md``):

  1. **SEED** — :func:`build_fixture` writes a tiny multi-module repo and builds
     the model, planting the three kinds of retained state the system claims:
     a descriptive ownership belief (cited), a confirmed compiled forbidden-edge
     invariant (HARD), and a rejected-pattern decision.
  2. **TEARDOWN** — :meth:`CodebaseModel.teardown_substrate` deletes *every*
     regenerable thing (nodes, edges, files). No call graph, no symbol table —
     the only way left to answer is the inferred layer.
  3. **PROBE** — each question is answered from beliefs/invariants ONLY. Where a
     belief carries a content-addressed citation, we re-fetch *exactly* that slice
     (:func:`citations.refetch`) from the still-on-disk repo to verify it resolves
     — the "re-fetch the cited slice, don't reconstruct" axis.
  4. **SCORE** — a probe passes iff it was answered from retained state AND (for
     cited beliefs) the cited slice still re-fetches AND its expectations hold.

The thesis in one line: drop the substrate, keep the learned cache, and the model
can still answer + verify from citations alone. If it can, the beliefs were
knowledge; if not, decoration.

Stdlib-only. Depends on :mod:`.service` (the composed model) and
:mod:`.citations` (the re-fetch primitive).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from . import citations
from .beliefs import slugify
from .citations import format_citation
from .model import BeliefKind, Enforcement
from .service import CodebaseModel


# --------------------------------------------------------------------------
# scoring result
# --------------------------------------------------------------------------
@dataclass
class AcceptanceResult:
    """The green/red bar plus the per-probe detail behind it. ``passed`` is the
    single gate; ``probes`` holds one scored dict per probe; ``score`` aggregates
    the doc-11 benchmark axes (coverage, re-fetch precision)."""
    passed: bool
    probes: list[dict] = field(default_factory=list)
    score: dict = field(default_factory=dict)


def _tokens(text: str) -> set[str]:
    """Lowercased alphanumeric tokens (>1 char) for fuzzy claim/topic overlap —
    same idiom the query surface uses so matching stays consistent."""
    return {t for t in re.split(r"[^a-z0-9]+", (text or "").lower()) if len(t) > 1}


class AcceptanceHarness:
    """Runs probes against a model whose substrate has been torn down, proving the
    answers come from the retained belief/invariant layer (and verify via cited
    slice re-fetch), not from a — now impossible — reconstruction."""

    def __init__(self, model: CodebaseModel):
        self.model = model

    # ------------------------------------------------------------------
    # the experiment
    # ------------------------------------------------------------------
    def run(self, probes: list[dict]) -> AcceptanceResult:
        """Execute the SEED→TEARDOWN→PROBE→SCORE loop over ``probes`` and return
        the aggregate bar. Each probe is ``{"kind", ...args..., "expect_contains",
        "expect_blocked"?}``; ``kind`` is ``"owns" | "allowed" | "affects"``."""
        model = self.model

        # (1) Pre-teardown: an "affects" answer is a *derived* fact off the live
        # call graph. The agent only retains it if it captures it as a belief
        # *before* the graph disappears — exactly the "learn it while you work"
        # move. We cite the target symbol so the belief stays re-fetchable.
        captured: dict[int, str] = {}
        for probe in probes:
            if probe.get("kind") == "affects":
                captured[id(probe)] = self._capture_affects(probe)

        # (2) Teardown: delete the entire regenerable substrate. Beliefs,
        # invariants and decisions survive — that's the whole bet.
        model.teardown_substrate()

        # (3)+(4) Answer each probe from the inferred layer alone, verify cited
        # slices via re-fetch, and score.
        results = [self._score_probe(p, captured.get(id(p))) for p in probes]
        passed = bool(results) and all(r["passed"] for r in results)
        return AcceptanceResult(passed=passed, probes=results,
                                score=self._aggregate(results))

    # ------------------------------------------------------------------
    # pre-teardown capture (affects)
    # ------------------------------------------------------------------
    def _capture_affects(self, probe: dict) -> str:
        """Read the live blast radius for an "affects" probe and freeze it into a
        cited descriptive belief, returning the belief id. The citation points at
        the target symbol's slice so post-teardown verification can re-fetch it."""
        model = self.model
        target = str(probe.get("target", ""))
        live = model.affects(target)
        files = live.get("affected_files", [])
        # cite the target symbol itself (file @ qualname) — that's the slice the
        # agent would re-read before editing.
        cit = self._citation_for(target)
        claim = f"(affects {target} {' '.join(files)})".strip()
        bid = "affects-" + slugify(target)
        model.beliefs.add(
            claim, confidence=0.85,
            justified_by=[cit] if cit else None,
            source="inferred", belief_id=bid, kind=BeliefKind.DESCRIPTIVE)
        return bid

    def _citation_for(self, target: str) -> str:
        """Best-effort content-addressed citation for ``target``: resolve it to a
        node (so we get its file + qualname) and format ``file @ symbol``. Falls
        back to a bare-file citation for a ``*.py`` target, else ``""``."""
        node = self.model.store.get_node(target)
        if node is None:
            hits = self.model.store.find_nodes_by_name(target)
            node = hits[0] if hits else None
        if node is not None:
            return format_citation(node.path, node.name)
        if target.endswith(".py"):
            return format_citation(target)
        return ""

    # ------------------------------------------------------------------
    # post-teardown scoring
    # ------------------------------------------------------------------
    def _score_probe(self, probe: dict, captured_bid: str | None) -> dict:
        """Answer one probe from the inferred layer and score it. Returns a dict
        with ``answered`` (from retained state), ``refetch_found`` (None when the
        answer carries no citation), ``blocking`` (allowed-probes only),
        ``contains_ok``, ``answer`` text, and the final ``passed``."""
        kind = probe.get("kind")
        if kind == "owns":
            res = self._answer_owns(probe)
        elif kind == "affects":
            res = self._answer_affects(probe, captured_bid)
        elif kind == "allowed":
            res = self._answer_allowed(probe)
        else:
            res = {"kind": kind, "answered": False, "refetch_found": None,
                   "blocking": None, "answer": f"unknown probe kind {kind!r}"}

        expect = probe.get("expect_contains", []) or []
        answer = res.get("answer", "")
        res["expect_contains"] = list(expect)
        res["contains_ok"] = all(s in answer for s in expect)

        # the three pass-axes, collapsed: answered-from-model, cited-slice
        # re-fetched (when applicable), and expectations (substrings + block bit).
        refetch_ok = res["refetch_found"] in (True, None)
        block_ok = True
        if "expect_blocked" in probe:
            block_ok = bool(res.get("blocking")) == bool(probe["expect_blocked"])
        res["block_ok"] = block_ok
        res["passed"] = bool(
            res["answered"] and res["contains_ok"] and refetch_ok and block_ok)
        return res

    def _answer_owns(self, probe: dict) -> dict:
        """Descriptive recall: answer from retained ownership beliefs and re-fetch
        each belief's cited slice to confirm it still resolves post-teardown."""
        topic = str(probe.get("topic", probe.get("target", "")))
        res = self.model.owns(topic)
        beliefs = res.get("beliefs", [])
        answered = bool(beliefs)
        refetch_found: bool | None = None
        if answered:
            refetch_found = True
            for b in beliefs:
                for raw in b.get("citations", []):
                    if not citations.refetch(citations.parse(raw),
                                             self.model.repo_root).found:
                        refetch_found = False
        return {"kind": "owns", "answered": answered,
                "refetch_found": refetch_found, "blocking": None,
                "answer": res.get("answer", "")}

    def _answer_affects(self, probe: dict, bid: str | None) -> dict:
        """Impact recall: read back the belief we captured pre-teardown and verify
        its citation re-fetches (the agent re-reading the cited slice before it
        edits)."""
        belief = self.model.beliefs.get(bid) if bid else None
        answered = belief is not None
        refetch_found: bool | None = None
        answer = belief.claim if belief else ""
        if belief is not None and belief.justified_by:
            refetch_found = all(
                citations.refetch(citations.parse(c), self.model.repo_root).found
                for c in belief.justified_by)
        return {"kind": "affects", "answered": answered,
                "refetch_found": refetch_found, "blocking": None,
                "answer": answer}

    def _answer_allowed(self, probe: dict) -> dict:
        """Prescriptive recall: the *rule* survives teardown even though the graph
        it checks does not. Match the description against retained invariants and
        decisions; "blocked" is decided by ``may_hard_block()`` on the rule, not by
        re-running the (now-gone) compiled check."""
        desc = str(probe.get("description", ""))
        toks = _tokens(desc)
        parts: list[str] = []
        blocking = False
        answered = False
        for inv in self.model.store.all_invariants():
            if toks & _tokens(f"{inv.claim} {inv.check}"):
                answered = True
                parts.append(inv.claim)
                if inv.check:
                    parts.append(inv.check)
                if inv.may_hard_block():
                    blocking = True
        for dec in self.model.store.all_decisions():
            if toks & _tokens(f"{dec.reason} {dec.detector}"):
                answered = True
                parts.append(dec.reason)
        return {"kind": "allowed", "answered": answered,
                "refetch_found": None, "blocking": blocking,
                "answer": " ; ".join(parts)}

    # ------------------------------------------------------------------
    # aggregate benchmark axes (doc 11)
    # ------------------------------------------------------------------
    def _aggregate(self, results: list[dict]) -> dict:
        """The trackable benchmark: how many probes passed, what fraction were
        answerable from the model (coverage), and — over the cited probes — what
        fraction re-fetched cleanly (re-fetch precision)."""
        total = len(results)
        passed_n = sum(1 for r in results if r["passed"])
        answered_n = sum(1 for r in results if r["answered"])
        cited = [r for r in results if r["refetch_found"] is not None]
        refetch_n = sum(1 for r in cited if r["refetch_found"])
        return {
            "total": total,
            "passed": passed_n,
            "coverage": (answered_n / total) if total else 0.0,
            "refetch_precision": (refetch_n / len(cited)) if cited else 1.0,
        }


# --------------------------------------------------------------------------
# the fixture repo + seeded model
# --------------------------------------------------------------------------
# A tiny but real architecture: billing owns refunds (which flush through a unit
# of work), render owns the Renderer, and plugins are forbidden from touching the
# Renderer directly. Written to disk so citation re-fetch has a real slice to pull.
_FIXTURE_FILES = {
    "billing/__init__.py": "",
    "billing/uow.py": (
        "\"\"\"The unit-of-work all persistence flows through.\"\"\"\n"
        "\n\n"
        "class UnitOfWork:\n"
        "    def __init__(self):\n"
        "        self._dirty = []\n\n"
        "    def commit(self):\n"
        "        # the single sanctioned write boundary\n"
        "        self._dirty.clear()\n"
        "        return True\n"
    ),
    "billing/refunds.py": (
        "\"\"\"Refunds — owned by billing, settled through the unit of work.\"\"\"\n"
        "from billing.uow import UnitOfWork\n"
        "\n\n"
        "class RefundService:\n"
        "    def __init__(self, uow):\n"
        "        self.uow = uow\n\n"
        "    def refund(self, payment_id, amount):\n"
        "        # issue a refund and settle it via the unit of work\n"
        "        self.uow.commit()\n"
        "        return {'payment': payment_id, 'amount': amount}\n"
    ),
    "billing/invoicing.py": (
        "\"\"\"Invoicing leans on RefundService — a downstream caller of refund.\"\"\"\n"
        "from billing.refunds import RefundService\n"
        "\n\n"
        "def void_invoice(invoice, svc):\n"
        "    # voiding an invoice issues a refund for its total\n"
        "    return svc.refund(invoice['payment'], invoice['total'])\n"
    ),
    "render/__init__.py": "",
    "render/renderer.py": (
        "\"\"\"The Renderer — owned by render; plugins must not touch it directly.\"\"\"\n"
        "\n\n"
        "class Renderer:\n"
        "    def render(self, template, ctx):\n"
        "        return f'<{template}>{ctx}</{template}>'\n"
    ),
    "plugins/__init__.py": "",
    "plugins/widget.py": (
        "\"\"\"A plugin. It currently *violates* the no-plugin-Renderer rule.\"\"\"\n"
        "from render.renderer import Renderer\n"
        "\n\n"
        "def build_widget(ctx):\n"
        "    # forbidden: a plugin constructing the Renderer directly\n"
        "    r = Renderer()\n"
        "    return r.render('widget', ctx)\n"
    ),
}


def build_fixture(root: Path) -> CodebaseModel:
    """Write the fixture repo under ``root``, build the model over it, and seed the
    three kinds of retained state the acceptance test probes:

      * a **descriptive ownership belief** (``billing owns refunds``) with a
        content-addressed citation to ``RefundService.refund``;
      * a **confirmed, compiled forbidden-edge invariant** (``plugins → Renderer``,
        HARD — eligible to hard-block);
      * a **rejected-pattern decision** (no global singleton ``Cache``).

    Returns the built model. Used by the tests and runnable as a demo. The db is
    placed *inside* ``root`` (``.cbm.db``) so the fixture is fully self-contained
    and never touches the user's real model store."""
    root = Path(root)
    for rel, content in _FIXTURE_FILES.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)

    model = CodebaseModel(root, db_path=root / ".cbm.db")
    model.build()

    # -- descriptive: who owns refunds (cited, so it survives teardown) --------
    model.beliefs.add(
        "(owns billing refunds)", confidence=0.9, source="human",
        justified_by=[format_citation("billing/refunds.py", "RefundService.refund")],
        belief_id="owns-billing-refunds",  # stable id the acceptance probes look up
        kind=BeliefKind.DESCRIPTIVE)

    # -- prescriptive: plugins must never reach the Renderer (HARD, compiled) --
    model.invariants.add(
        "plugins must not construct or call render.Renderer",
        check="(forbidden-edge :kind calls :from plugins :to Renderer)",
        enforcement=Enforcement.HARD, confidence="confirmed", source="human")

    # -- decision: the rejected global-singleton pattern -----------------------
    model.invariants.add_decision(
        reason="global singleton Cache was rejected; resolve it via the registry",
        detector="(construct Cache)",
        accepted_pattern="registry.get('cache')")

    return model
