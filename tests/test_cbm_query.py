"""Behavioural tests for the query surface (core/codebase_model/query.py).

Builds a tiny real fixture repo on disk, indexes it with the real substrate
parser, seeds beliefs/invariants/decisions through their managers, and exercises
QuerySurface end to end: blast-radius impact, descriptive ownership recall (with
LRU touch), prescriptive allowance (HARD blocking vs SOFT), the architecture
index, per-node fact projection, and the free-text router.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from tether.core.codebase_model.beliefs import BeliefManager
from tether.core.codebase_model.indexer import Indexer
from tether.core.codebase_model.invariants import InvariantEngine
from tether.core.codebase_model.model import BeliefKind, Enforcement
from tether.core.codebase_model.query import QuerySurface, _subject, _tokens
from tether.core.codebase_model.store import ModelStore


# billing: a RefundService with a refund() that several call sites reach
# transitively (api.handle -> refund, api.outer -> handle).
REFUNDS = """\
class RefundService:
    def refund(self):
        return self.retry()

    def retry(self):
        return 1
"""

API = """\
from billing.refunds import RefundService


def handle():
    return RefundService().refund()


def outer():
    return handle()
"""

# plugins must not reach Renderer (a forbidden cross-module edge).
WIDGET = """\
from render.engine import Renderer


def show():
    return Renderer().draw()
"""

ENGINE = """\
class Renderer:
    def draw(self):
        return 1
"""

# a module-scope construction of Cache — the rejected pattern.
CACHEMOD = """\
class Cache:
    pass


CACHE = Cache()
"""


class _Clock:
    """Deterministic injectable clock so LRU touches are observable."""
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class QuerySurfaceTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.files = {
            "billing/refunds.py": REFUNDS,
            "billing/api.py": API,
            "plugins/widget.py": WIDGET,
            "render/engine.py": ENGINE,
            "cache/glob.py": CACHEMOD,
        }
        for rel, content in self.files.items():
            full = os.path.join(self.dir, rel)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w") as fh:
                fh.write(content)
        self.store = ModelStore(os.path.join(self.dir, "model.db"))
        self.indexer = Indexer(self.store, self.dir)
        self.indexer.cold_build()
        self.store.set_meta("commit", "deadbee")
        self.clock = _Clock()
        self.beliefs = BeliefManager(self.store, clock=self.clock)
        self.invariants = InvariantEngine(self.store)
        self.q = QuerySurface(self.store, self.indexer, self.beliefs, self.invariants)

    def tearDown(self):
        self.store.close()

    def _node_id(self, name):
        nodes = self.store.find_nodes_by_name(name)
        self.assertTrue(nodes, f"no node named {name!r}")
        return nodes[0].id

    # -- helpers -----------------------------------------------------------
    def test_tokens_and_subject_extraction(self):
        self.assertEqual(_tokens("(owns billing refunds)"), {"owns", "billing", "refunds"})
        # dotted/CamelCase wins as the subject
        self.assertEqual(_subject("what does changing RefundService.refund affect?"),
                         "RefundService.refund")
        # else the longest non-stopword bare word
        self.assertEqual(_subject("what owns refunds?"), "refunds")

    # -- affects (blast radius) -------------------------------------------
    def test_affects_finds_transitive_callers(self):
        res = self.q.affects("refund")
        # handle() calls refund; outer() calls handle -> both reachable
        joined = " ".join(res["affected_symbols"])
        self.assertIn("handle", joined)
        self.assertIn("outer", joined)
        self.assertEqual(res["count"], len(res["affected_symbols"]))
        self.assertIn("billing/api.py", res["affected_files"])
        # the target's own definition is never in its own blast radius
        self.assertNotIn(self._node_id("RefundService.refund"), res["affected_symbols"])

    def test_affects_by_file_path(self):
        res = self.q.affects("billing/refunds.py")
        # editing refunds.py reaches the api callers
        self.assertIn("billing/api.py", res["affected_files"])
        self.assertTrue(res["count"] >= 1)

    def test_affects_unknown_target_is_empty(self):
        res = self.q.affects("does_not_exist")
        self.assertEqual(res["affected_symbols"], [])
        self.assertEqual(res["affected_files"], [])
        self.assertEqual(res["count"], 0)

    # -- owns (descriptive recall) ----------------------------------------
    def test_owns_recalls_and_touches_lru(self):
        self.clock.t = 1000.0
        seeded = self.beliefs.add(
            "(owns billing refunds)", confidence=0.9,
            justified_by=["billing/refunds.py @ RefundService.refund @ deadbee"])
        # an unrelated descriptive belief that must NOT surface
        self.beliefs.add("(owns render drawing)", confidence=0.8)
        before = self.store.get_belief(seeded.id).last_consulted

        self.clock.t = 2000.0  # advance so the consult touch is observable
        res = self.q.owns("refunds")
        self.assertEqual(len(res["beliefs"]), 1)
        hit = res["beliefs"][0]
        self.assertEqual(hit["id"], seeded.id)
        self.assertEqual(hit["citations"],
                         ["billing/refunds.py @ RefundService.refund @ deadbee"])
        self.assertFalse(hit["stale"])
        self.assertIn("refunds", res["answer"])
        # consult() must have LRU-touched the belief
        after = self.store.get_belief(seeded.id).last_consulted
        self.assertEqual(after, 2000.0)
        self.assertGreater(after, before)

    def test_owns_no_belief(self):
        res = self.q.owns("refunds")
        self.assertEqual(res["beliefs"], [])
        self.assertIn("No retained belief", res["answer"])

    def test_owns_surfaces_stale_flag(self):
        self.beliefs.add("(owns billing refunds)", confidence=0.9,
                         justified_by=["billing/refunds.py @ RefundService.refund"])
        self.beliefs.invalidate(["billing/refunds.py"])  # marks the belief stale
        res = self.q.owns("refunds")
        self.assertTrue(res["beliefs"][0]["stale"])

    def test_owns_ignores_non_descriptive(self):
        # a prescriptive-kind belief mentioning "own" must not be recalled as owns
        self.beliefs.add("(owns billing refunds)", confidence=0.9,
                         kind=BeliefKind.PRESCRIPTIVE, belief_id="presc-own")
        res = self.q.owns("refunds")
        self.assertEqual(res["beliefs"], [])

    # -- allowed (prescriptive + rejected patterns) -----------------------
    def test_allowed_hard_invariant_blocks(self):
        self.invariants.add(
            "plugins must not call Renderer",
            check="(forbidden-edge :kind calls :from plugins :to Renderer)",
            enforcement=Enforcement.HARD, confidence="confirmed")
        res = self.q.allowed("call Renderer from a plugin",
                             changed_files=["plugins/widget.py"])
        self.assertFalse(res["allowed"])
        self.assertTrue(res["blocking"])
        self.assertTrue(any(v["blocking"] for v in res["violations"]))
        # location resolves to the offending src file:line
        loc = res["violations"][0]["location"]
        self.assertTrue(loc.startswith("plugins/widget.py:"))

    def test_allowed_soft_invariant_does_not_block(self):
        # same check but SOFT + numeric confidence => may_hard_block() is False
        self.invariants.add(
            "plugins should avoid Renderer",
            check="(forbidden-edge :kind calls :from plugins :to Renderer)",
            enforcement=Enforcement.SOFT, confidence=0.6)
        res = self.q.allowed("call Renderer from a plugin",
                             changed_files=["plugins/widget.py"])
        self.assertTrue(res["allowed"])          # not blocking
        self.assertFalse(res["blocking"])
        self.assertTrue(res["violations"])       # but still surfaced as a warning

    def test_allowed_uncompiled_invariant_skipped(self):
        # no check => nothing deterministic to run => no violation
        self.invariants.add("renderers should be tidy", check="",
                            enforcement=Enforcement.HARD)
        res = self.q.allowed("anything", changed_files=["plugins/widget.py"])
        self.assertTrue(res["allowed"])
        self.assertEqual(res["violations"], [])

    def test_allowed_detects_rejected_pattern(self):
        self.invariants.add_decision(
            reason="singleton cache caused stale state",
            detector="(construct Cache)")
        res = self.q.allowed("construct a singleton cache",
                             changed_files=["cache/glob.py"])
        # a rejected-pattern detection is SOFT, so it surfaces but does not block
        self.assertTrue(res["violations"])
        self.assertFalse(res["blocking"])
        self.assertIn("singleton cache", res["violations"][0]["claim"])

    def test_allowed_clean_diff(self):
        self.invariants.add(
            "plugins must not call Renderer",
            check="(forbidden-edge :kind calls :from plugins :to Renderer)",
            enforcement=Enforcement.HARD, confidence="confirmed")
        # a diff that doesn't touch the plugin => no violation over that diff
        res = self.q.allowed("edit billing", changed_files=["billing/api.py"])
        self.assertTrue(res["allowed"])
        self.assertEqual(res["violations"], [])

    # -- architecture index ------------------------------------------------
    def test_architecture_index_summary(self):
        self.beliefs.add("(owns billing refunds)", confidence=0.9)
        self.invariants.add(
            "plugins must not call Renderer",
            check="(forbidden-edge :kind calls :from plugins :to Renderer)",
            enforcement=Enforcement.HARD, confidence="confirmed")
        idx = self.q.architecture_index()
        self.assertIn("(architecture", idx)
        self.assertIn("billing", idx)
        self.assertIn("(beliefs 1)", idx)
        self.assertIn("forbidden-edge", idx)  # invariant rendered as its check s-expr
        self.assertLess(len(idx), 2048)

    def test_architecture_index_skips_soft_uncompiled(self):
        self.invariants.add("vague rule", check="", enforcement=Enforcement.SOFT,
                            confidence=0.5)
        idx = self.q.architecture_index()
        self.assertIn("(invariants)", idx)  # nothing compiled/confirmed to list

    # -- project_facts -----------------------------------------------------
    def test_project_facts_emits_call_facts(self):
        handle_id = self._node_id("handle")
        facts = self.q.project_facts(handle_id)
        # handle() calls refund (and constructs RefundService)
        self.assertIn("(fact (calls handle", facts)
        self.assertIn("refund", facts)
        self.assertIn(':at "deadbee"', facts)

    # -- free-text routing -------------------------------------------------
    def test_answer_routes_affects(self):
        out = self.q.answer("what does changing refund affect?")
        self.assertIn("affects", out)
        self.assertIn("billing/api.py", out)

    def test_answer_routes_owns(self):
        self.beliefs.add("(owns billing refunds)", confidence=0.9)
        out = self.q.answer("what owns refunds?")
        self.assertIn("owns billing refunds", out)

    def test_answer_routes_allowed_blocking(self):
        self.invariants.add(
            "plugins must not call Renderer",
            check="(forbidden-edge :kind calls :from plugins :to Renderer)",
            enforcement=Enforcement.HARD, confidence="confirmed")
        out = self.q.answer("is it allowed to call Renderer? (pattern)")
        self.assertIn("Not allowed", out)

    def test_answer_defaults_to_architecture(self):
        out = self.q.answer("give me an overview of the system")
        self.assertIn("(architecture", out)


if __name__ == "__main__":
    unittest.main()
