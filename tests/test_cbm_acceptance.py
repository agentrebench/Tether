"""Behavioural tests for the acceptance harness (core/codebase_model/acceptance.py).

These exercise the doc-11 thesis end to end against a real fixture repo on disk:
seed beliefs/invariants, tear down the entire substrate, then prove every probe is
answered from the inferred layer alone and that cited slices still re-fetch from
the (now substrate-less) repo. Nothing here mocks the model — it drives the real
CodebaseModel built by build_fixture.
"""
import os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

import shutil
import unittest
from pathlib import Path

from tether.core.codebase_model.acceptance import (
    AcceptanceHarness, AcceptanceResult, build_fixture)
from tether.core.codebase_model.model import BeliefKind, Enforcement
from tether.core.codebase_model import citations


class _Base(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="cbm-accept-"))
        self.model = build_fixture(self.root)

    def tearDown(self):
        try:
            self.model.close()
        except Exception:
            pass
        shutil.rmtree(self.root, ignore_errors=True)


# --------------------------------------------------------------------------
# build_fixture — the seed
# --------------------------------------------------------------------------
class BuildFixtureTests(_Base):
    def test_files_written_to_disk(self):
        for rel in ("billing/refunds.py", "billing/uow.py", "billing/invoicing.py",
                    "render/renderer.py", "plugins/widget.py"):
            self.assertTrue((self.root / rel).is_file(), rel)

    def test_substrate_built(self):
        # the cold build parsed the fixture into nodes/edges.
        self.assertGreater(self.model.store.count_nodes(), 0)
        self.assertTrue(self.model.store.all_files())

    def test_ownership_belief_seeded_and_cited(self):
        b = self.model.beliefs.get("owns-billing-refunds")
        self.assertIsNotNone(b)
        self.assertEqual(b.kind, BeliefKind.DESCRIPTIVE)
        self.assertTrue(b.justified_by, "ownership belief must carry a citation")
        self.assertIn("billing/refunds.py", b.justified_by[0])

    def test_forbidden_edge_invariant_seeded_hard_and_compiled(self):
        invs = self.model.store.all_invariants()
        self.assertEqual(len(invs), 1)
        inv = invs[0]
        self.assertTrue(inv.compiled)
        self.assertTrue(inv.confirmed)
        self.assertEqual(inv.enforcement, Enforcement.HARD)
        # confirmed + compiled + HARD ⇒ may hard-block.
        self.assertTrue(inv.may_hard_block())

    def test_invariant_actually_fires_before_teardown(self):
        # the seeded plugin really does violate the rule, with a real location.
        vios = self.model.invariants.check_all()
        self.assertEqual(len(vios), 1)
        self.assertTrue(vios[0].location.startswith("plugins/widget.py:"))
        self.assertTrue(vios[0].blocking)

    def test_rejected_decision_seeded(self):
        decs = self.model.store.all_decisions()
        self.assertEqual(len(decs), 1)
        self.assertEqual(decs[0].detector, "(construct Cache)")
        self.assertEqual(decs[0].status, "rejected")


# --------------------------------------------------------------------------
# the core experiment — teardown then answer from the inferred layer
# --------------------------------------------------------------------------
class HarnessRunTests(_Base):
    def _probes(self):
        return [
            {"kind": "owns", "topic": "refunds",
             "expect_contains": ["billing", "refunds"]},
            {"kind": "affects", "target": "RefundService.refund",
             "expect_contains": ["billing/invoicing.py"]},
            {"kind": "allowed",
             "description": "construct a Renderer inside a plugin",
             "expect_contains": ["Renderer"], "expect_blocked": True},
        ]

    def test_all_probes_pass(self):
        harness = AcceptanceHarness(self.model)
        result = harness.run(self._probes())
        self.assertIsInstance(result, AcceptanceResult)
        self.assertTrue(result.passed,
                        msg=f"probes: {result.probes}")
        self.assertEqual(result.score["passed"], 3)
        self.assertEqual(result.score["total"], 3)
        self.assertEqual(result.score["coverage"], 1.0)
        self.assertEqual(result.score["refetch_precision"], 1.0)

    def test_substrate_is_actually_gone_after_run(self):
        AcceptanceHarness(self.model).run(self._probes())
        # the derived layer was torn down...
        self.assertEqual(self.model.store.count_nodes(), 0)
        self.assertEqual(self.model.store.all_edges(), [])
        self.assertEqual(self.model.store.all_files(), [])
        # ...but the inferred layer survived.
        self.assertTrue(self.model.beliefs.all())
        self.assertTrue(self.model.store.all_invariants())

    def test_owns_answered_from_belief_with_refetch(self):
        # answered purely from the descriptive belief + cited slice re-fetch.
        harness = AcceptanceHarness(self.model)
        result = harness.run([
            {"kind": "owns", "topic": "refunds", "expect_contains": ["refunds"]}])
        probe = result.probes[0]
        self.assertTrue(probe["answered"])
        self.assertIs(probe["refetch_found"], True)
        self.assertTrue(probe["passed"])

    def test_affects_captured_pre_teardown_then_recalled(self):
        harness = AcceptanceHarness(self.model)
        result = harness.run([
            {"kind": "affects", "target": "RefundService.refund",
             "expect_contains": ["billing/invoicing.py"]}])
        probe = result.probes[0]
        # the live blast radius (invoicing calls refund) was frozen into a belief.
        self.assertTrue(probe["answered"])
        self.assertIn("billing/invoicing.py", probe["answer"])
        self.assertIs(probe["refetch_found"], True)
        # and the belief is durable in the inferred layer post-teardown.
        b = self.model.beliefs.get("affects-refundservice-refund")
        self.assertIsNotNone(b)
        self.assertTrue(b.justified_by)

    def test_allowed_blocked_from_invariant_not_substrate(self):
        # the graph is gone, yet the *rule* still blocks: answered from the
        # retained, may-hard-block invariant.
        harness = AcceptanceHarness(self.model)
        result = harness.run([
            {"kind": "allowed",
             "description": "let a plugin call Renderer directly",
             "expect_contains": ["Renderer"], "expect_blocked": True}])
        probe = result.probes[0]
        self.assertTrue(probe["answered"])
        self.assertTrue(probe["blocking"])
        self.assertTrue(probe["passed"])

    def test_allowed_unmatched_description_is_not_blocked(self):
        # a description that matches no retained rule is not answered-as-blocked.
        harness = AcceptanceHarness(self.model)
        result = harness.run([
            {"kind": "allowed",
             "description": "rename a local helper variable",
             "expect_contains": [], "expect_blocked": False}])
        probe = result.probes[0]
        self.assertFalse(probe["blocking"])
        # answered is False (no rule matched) so this probe fails the answered axis.
        self.assertFalse(probe["answered"])
        self.assertFalse(probe["passed"])


# --------------------------------------------------------------------------
# the failure modes the score must catch
# --------------------------------------------------------------------------
class ScoringTests(_Base):
    def test_unmet_expect_contains_fails_probe(self):
        harness = AcceptanceHarness(self.model)
        result = harness.run([
            {"kind": "owns", "topic": "refunds",
             "expect_contains": ["definitely-not-present"]}])
        probe = result.probes[0]
        self.assertTrue(probe["answered"])          # belief was recalled
        self.assertFalse(probe["contains_ok"])      # but the expectation missed
        self.assertFalse(probe["passed"])
        self.assertFalse(result.passed)

    def test_wrong_block_expectation_fails_probe(self):
        # rule really does block, but the probe expected it to be allowed.
        harness = AcceptanceHarness(self.model)
        result = harness.run([
            {"kind": "allowed",
             "description": "plugin constructs Renderer",
             "expect_contains": [], "expect_blocked": False}])
        probe = result.probes[0]
        self.assertTrue(probe["blocking"])
        self.assertFalse(probe["block_ok"])
        self.assertFalse(probe["passed"])

    def test_demoted_belief_breaks_refetch_axis(self):
        # Citation points at a slice that no longer exists ⇒ refetch fails ⇒
        # the probe cannot pass on the re-fetch axis. We simulate drift by
        # re-pointing the belief's citation at a vanished symbol *before* teardown.
        from tether.core.codebase_model.citations import format_citation
        b = self.model.beliefs.get("owns-billing-refunds")
        b.justified_by = [format_citation("billing/refunds.py", "GoneService.poof")]
        self.model.store.put_belief(b)
        harness = AcceptanceHarness(self.model)
        result = harness.run([
            {"kind": "owns", "topic": "refunds", "expect_contains": ["refunds"]}])
        probe = result.probes[0]
        self.assertTrue(probe["answered"])
        self.assertIs(probe["refetch_found"], False)
        self.assertFalse(probe["passed"])

    def test_empty_probes_does_not_pass(self):
        result = AcceptanceHarness(self.model).run([])
        self.assertFalse(result.passed)
        self.assertEqual(result.score["total"], 0)

    def test_unknown_probe_kind_is_unanswered(self):
        result = AcceptanceHarness(self.model).run([
            {"kind": "nonsense", "expect_contains": []}])
        probe = result.probes[0]
        self.assertFalse(probe["answered"])
        self.assertFalse(probe["passed"])

    def test_refetch_still_resolves_from_disk_after_teardown(self):
        # direct check of the thesis primitive: substrate DB rows gone, slice
        # re-fetched straight from the on-disk repo.
        self.model.teardown_substrate()
        self.assertEqual(self.model.store.count_nodes(), 0)
        cit = citations.parse(
            "billing/refunds.py @ RefundService.refund")
        res = citations.refetch(cit, self.model.repo_root)
        self.assertTrue(res.found)
        self.assertIn("def refund", res.source)


if __name__ == "__main__":
    unittest.main()
