import os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

import unittest
from pathlib import Path

from tether.core.codebase_model.beliefs import BeliefManager, slugify
from tether.core.codebase_model.store import ModelStore
from tether.core.codebase_model.model import BeliefKind, OnConflict


class _Clock:
    """Deterministic, monotonically advancing clock for LRU/verified stamps."""
    def __init__(self, start=1000.0):
        self.t = start

    def __call__(self):
        self.t += 1.0
        return self.t


def _mgr(**kw):
    d = tempfile.mkdtemp()
    store = ModelStore(Path(d) / "model.db")
    return BeliefManager(store, clock=_Clock(), **kw), store, d


class TestSlugify(unittest.TestCase):
    def test_kebab(self):
        self.assertEqual(slugify("(owns billing refunds)"), "owns-billing-refunds")
        self.assertEqual(slugify("   "), "belief")


class TestAddAndSupersede(unittest.TestCase):
    def test_add_defaults(self):
        mgr, store, _ = _mgr()
        b = mgr.add("(owns billing refunds)")
        # readable slug prefix + 8-hex hash of the normalized claim
        self.assertRegex(b.id, r"^owns-billing-refunds-[0-9a-f]{8}$")
        self.assertEqual(b.kind, BeliefKind.DESCRIPTIVE)
        self.assertEqual(b.on_conflict, OnConflict.DEMOTE)
        self.assertEqual(b.confirmations, 1)
        self.assertFalse(b.stale)
        self.assertTrue(b.verified)              # ISO stamp set via clock
        self.assertGreater(b.last_consulted, 0)  # protected from immediate LRU
        self.assertEqual(store.count_beliefs(), 1)

    def test_explicit_id(self):
        mgr, _, _ = _mgr()
        b = mgr.add("anything", belief_id="my-id")
        self.assertEqual(b.id, "my-id")

    def test_distinct_wordings_do_not_collide(self):
        # "uses X." and "uses X?" slugify identically; ids must still differ
        # so one claim can't silently supersede a different one.
        mgr, store, _ = _mgr()
        a = mgr.add("uses X.")
        b = mgr.add("uses X?")
        self.assertNotEqual(a.id, b.id)
        self.assertEqual(store.count_beliefs(), 2)

    def test_identical_restatement_still_supersedes(self):
        mgr, store, _ = _mgr()
        first = mgr.add("parser   returns a dict")
        second = mgr.add("parser returns a dict")  # whitespace-normalized match
        self.assertEqual(first.id, second.id)
        self.assertEqual(store.count_beliefs(), 1)
        self.assertEqual(second.confirmations, 2)

    def test_supersession_in_place(self):
        mgr, store, _ = _mgr()
        first = mgr.add("(owns billing refunds)", confidence=0.7,
                        justified_by=["billing/refunds.py @ refund"])
        created0 = first.created
        # restate with HIGHER confidence and fresh citation
        second = mgr.add("(owns billing refunds)", confidence=0.9,
                         justified_by=["billing/refunds.py @ refund2"])
        # one row only — superseded, not appended
        self.assertEqual(store.count_beliefs(), 1)
        # confidence rose in place, confirmations bumped, created preserved
        self.assertGreater(second.confidence, first.confidence)
        self.assertEqual(second.confirmations, 2)
        self.assertEqual(second.created, created0)
        self.assertIn("billing/refunds.py @ refund2", second.justified_by)

    def test_supersession_never_lowers_confidence(self):
        mgr, _, _ = _mgr()
        a = mgr.add("(x)", confidence=0.8)
        b = mgr.add("(x)", confidence=0.3)  # lower restatement
        self.assertGreaterEqual(b.confidence, a.confidence)
        self.assertEqual(b.confirmations, 2)

    def test_supersession_keeps_old_citations_when_none_given(self):
        mgr, _, _ = _mgr()
        mgr.add("(x)", confidence=0.6, justified_by=["a.py @ f"])
        b = mgr.add("(x)", confidence=0.7)
        self.assertEqual(b.justified_by, ["a.py @ f"])


class TestConsult(unittest.TestCase):
    def test_consult_touches_lru(self):
        mgr, store, _ = _mgr()
        b0 = mgr.add("(x)", belief_id="x")
        t0 = store.get_belief("x").last_consulted
        b1 = mgr.consult("x")
        self.assertIsNotNone(b1)
        self.assertGreater(store.get_belief("x").last_consulted, t0)

    def test_consult_missing(self):
        mgr, _, _ = _mgr()
        self.assertIsNone(mgr.consult("nope"))


class TestReinforceDemote(unittest.TestCase):
    def test_reinforce_raises_and_clears_stale(self):
        mgr, store, _ = _mgr()
        mgr.add("(x)", confidence=0.5, belief_id="x")
        store.mark_beliefs_stale(["x"])
        self.assertTrue(store.get_belief("x").stale)
        b = mgr.reinforce("x")
        self.assertGreater(b.confidence, 0.5)
        self.assertFalse(b.stale)
        self.assertEqual(b.confirmations, 2)

    def test_demote_lowers(self):
        mgr, _, _ = _mgr()
        mgr.add("(x)", confidence=0.8, belief_id="x")
        b = mgr.demote("x", amount=0.25)
        self.assertIsNotNone(b)
        self.assertAlmostEqual(b.confidence, 0.55)

    def test_demote_below_floor_deletes(self):
        mgr, store, _ = _mgr()
        mgr.add("(x)", confidence=0.3, belief_id="x")
        result = mgr.demote("x", amount=0.25)  # 0.05 < floor 0.15
        self.assertIsNone(result)
        self.assertIsNone(store.get_belief("x"))

    def test_demote_missing(self):
        mgr, _, _ = _mgr()
        self.assertIsNone(mgr.demote("nope"))


class TestInvalidate(unittest.TestCase):
    def test_invalidate_marks_citing_beliefs_stale(self):
        mgr, store, _ = _mgr()
        seeded = mgr.add("(owns billing refunds)",
                         justified_by=["billing/refunds.py @ refund"])
        other = mgr.add("(unrelated)", justified_by=["other/thing.py @ x"])
        ids = mgr.invalidate(["billing/refunds.py"])
        self.assertEqual(ids, [seeded.id])
        self.assertTrue(store.get_belief(seeded.id).stale)
        self.assertFalse(store.get_belief(other.id).stale)

    def test_invalidate_dedupes(self):
        mgr, _, _ = _mgr()
        mgr.add("(x)", belief_id="b", justified_by=["a.py @ f", "a.py @ g"])
        ids = mgr.invalidate(["a.py", "a.py"])
        self.assertEqual(ids, ["b"])


class TestReverify(unittest.TestCase):
    def _repo(self):
        root = Path(tempfile.mkdtemp())
        (root / "mod.py").write_text(
            "def refund(x):\n    return x\n\n\ndef other(y):\n    return y\n")
        return root

    def test_reverify_reinforces_when_citation_resolves(self):
        mgr, store, _ = _mgr()
        root = self._repo()
        mgr.add("(owns refund)", belief_id="b", confidence=0.5,
                justified_by=["mod.py @ refund"])
        store.mark_beliefs_stale(["b"])
        ok = mgr.reverify("b", root)
        self.assertTrue(ok)
        b = store.get_belief("b")
        self.assertFalse(b.stale)            # cleared by reinforce
        self.assertGreater(b.confidence, 0.5)

    def test_reverify_demotes_when_citation_missing(self):
        mgr, store, _ = _mgr()
        root = self._repo()
        mgr.add("(owns gone)", belief_id="b", confidence=0.8,
                justified_by=["mod.py @ nonexistent_symbol"])
        ok = mgr.reverify("b", root)
        self.assertFalse(ok)
        self.assertAlmostEqual(store.get_belief("b").confidence, 0.55)  # demoted

    def test_reverify_demotes_when_file_missing(self):
        mgr, store, _ = _mgr()
        root = self._repo()
        mgr.add("(owns)", belief_id="b", confidence=0.8,
                justified_by=["does_not_exist.py @ refund"])
        self.assertFalse(mgr.reverify("b", root))
        self.assertAlmostEqual(store.get_belief("b").confidence, 0.55)

    def test_reverify_custom_verifier_false_demotes(self):
        d = tempfile.mkdtemp()
        store = ModelStore(Path(d) / "m.db")
        mgr = BeliefManager(store, clock=_Clock(),
                            verifier=lambda belief, results: False)
        root = self._repo()
        mgr.add("(owns refund)", belief_id="b", confidence=0.8,
                justified_by=["mod.py @ refund"])
        ok = mgr.reverify("b", root)
        self.assertFalse(ok)  # citation resolved but verifier rejected
        self.assertAlmostEqual(store.get_belief("b").confidence, 0.55)

    def test_reverify_missing_belief(self):
        mgr, _, _ = _mgr()
        self.assertFalse(mgr.reverify("nope", tempfile.mkdtemp()))


class TestEviction(unittest.TestCase):
    def test_cap_evicts_least_recently_consulted(self):
        mgr, store, _ = _mgr(max_beliefs=3)
        for i in range(3):
            mgr.add(f"(b {i})", belief_id=f"b{i}")
        # consult b0 so it is most-recently used and should survive
        mgr.consult("b0")
        mgr.add("(b 3)", belief_id="b3")  # triggers eviction back down to 3
        self.assertEqual(store.count_beliefs(), 3)
        ids = {b.id for b in mgr.all()}
        self.assertIn("b3", ids)   # freshly added survives
        self.assertIn("b0", ids)   # recently consulted survives
        self.assertNotIn("b1", ids)  # oldest untouched evicted

    def test_no_eviction_under_cap(self):
        mgr, store, _ = _mgr(max_beliefs=10)
        for i in range(5):
            mgr.add(f"(b {i})", belief_id=f"b{i}")
        self.assertEqual(store.count_beliefs(), 5)


if __name__ == "__main__":
    unittest.main()
