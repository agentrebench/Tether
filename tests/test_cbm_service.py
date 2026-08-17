"""Behavioural tests for the service facade (core/codebase_model/service.py).

Builds a tiny real fixture repo on disk and drives the composed CodebaseModel end
to end: the build/sync lifecycle, the never-raise edit hook, substrate teardown
that preserves the inferred layer, the per-root get_model cache, the read/record
delegates, and the tricky belief/invariant/indexer behaviours surfaced through
the service (supersession, demote-to-eviction, lazy re-verification, forbidden
edges with real locations, asymmetric enforcement, incremental indexing).
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from tether.core.codebase_model import service
from tether.core.codebase_model.service import (CodebaseModel, get_model,
                                                  model_db_path)
from tether.core.codebase_model.citations import format_citation
from tether.core.codebase_model.model import BeliefKind, Enforcement


# -- fixture sources -------------------------------------------------------
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


def _write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def _fixture(root: Path) -> None:
    _write(root, "billing/refunds.py", REFUNDS)
    _write(root, "api.py", API)
    _write(root, "plugins/widget.py", WIDGET)
    _write(root, "render/engine.py", ENGINE)


class ServiceTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = Path(self.tmp) / "repo"
        self.root.mkdir(parents=True, exist_ok=True)
        _fixture(self.root)
        self.db = Path(self.tmp) / "model.db"
        self.model = CodebaseModel(self.root, db_path=self.db)

    def tearDown(self):
        try:
            self.model.close()
        except Exception:
            pass


# --------------------------------------------------------------------------
# wiring + db path
# --------------------------------------------------------------------------
class TestWiringAndPath(ServiceTestBase):
    def test_components_share_one_store(self):
        m = self.model
        self.assertIs(m.indexer.store, m.store)
        self.assertIs(m.beliefs.store, m.store)
        self.assertIs(m.invariants.store, m.store)
        self.assertIs(m.query.store, m.store)
        self.assertIs(m.query.indexer, m.indexer)
        self.assertEqual(Path(m.repo_root), self.root)

    def test_model_db_path_is_deterministic_and_unique(self):
        a1 = model_db_path("/some/repo/root")
        a2 = model_db_path("/some/repo/root")
        b = model_db_path("/other/root")
        self.assertEqual(a1, a2)
        self.assertNotEqual(a1, b)
        # CONFIG_DIR/models/<16 hex>.db
        self.assertEqual(a1.parent.name, "models")
        self.assertTrue(a1.name.endswith(".db"))
        self.assertEqual(len(a1.stem), 16)

    def test_default_db_path_used_when_none(self):
        # construct against a redirected CONFIG_DIR so we don't touch ~/.tether
        orig = service.CONFIG_DIR
        service.CONFIG_DIR = Path(self.tmp) / "cfg"
        try:
            m = CodebaseModel(self.root)
            self.assertEqual(m.db_path, model_db_path(self.root))
            self.assertTrue(str(m.db_path).startswith(str(service.CONFIG_DIR)))
            m.close()
        finally:
            service.CONFIG_DIR = orig


# --------------------------------------------------------------------------
# build / sync lifecycle
# --------------------------------------------------------------------------
class TestLifecycle(ServiceTestBase):
    def test_build_indexes_repo_and_stamps_commit(self):
        result = self.model.build()
        self.assertEqual(result["files"], 4)
        self.assertEqual(result["indexed"], 4)
        self.assertGreater(self.model.store.count_nodes(), 0)
        # commit meta is set (empty string when no git, but the key exists)
        self.assertIsNotNone(self.model.store.get_meta("commit"))

    def test_build_is_idempotent_unchanged_files_are_noops(self):
        self.model.build()
        # a second build over an unchanged tree reindexes nothing
        again = self.model.build()
        self.assertEqual(again["indexed"], 0)
        self.assertEqual(again["files"], 4)

    def test_index_file_unchanged_returns_false(self):
        self.model.build()
        # re-indexing an unchanged file costs only a hash and is a no-op
        self.assertFalse(self.model.indexer.index_file("api.py"))

    def test_sync_is_incremental_and_invalidates_beliefs(self):
        self.model.build()
        # a belief that cites the file we're about to edit
        cite = format_citation("billing/refunds.py", "RefundService.refund")
        self.model.record_belief("(owns billing refunds)", confidence=0.8,
                                 justified_by=[cite])
        bid = self.model.beliefs.all()[0].id
        self.assertFalse(self.model.beliefs.get(bid).stale)

        # an edit that leaves the cited symbol's slice untouched must NOT
        # stale the belief (invalidation is symbol-granular)
        _write(self.root, "billing/refunds.py", REFUNDS + "\n# touched\n")
        result = self.model.sync()
        self.assertEqual(result["indexed"], 1)         # only the edited file
        self.assertEqual(result["removed"], 0)
        self.assertFalse(self.model.beliefs.get(bid).stale)

        # an edit inside the cited symbol does invalidate it
        _write(self.root, "billing/refunds.py",
               REFUNDS.replace("return self.retry()", "return self.retry() or 0")
               + "\n# touched\n")
        result = self.model.sync()
        self.assertEqual(result["indexed"], 1)
        self.assertEqual(result["invalidated"], 1)     # the citing belief
        self.assertTrue(self.model.beliefs.get(bid).stale)

    def test_sync_removes_deleted_files(self):
        self.model.build()
        self.assertTrue(self.model.store.nodes_in_file("plugins/widget.py"))
        (self.root / "plugins" / "widget.py").unlink()
        result = self.model.sync()
        self.assertEqual(result["removed"], 1)
        self.assertEqual(self.model.store.nodes_in_file("plugins/widget.py"), [])


# --------------------------------------------------------------------------
# on_edit — reindex + invalidate, and must never raise
# --------------------------------------------------------------------------
class TestOnEdit(ServiceTestBase):
    def test_on_edit_reindexes_and_invalidates(self):
        self.model.build()
        cite = format_citation("api.py", "handle")
        self.model.record_belief("(api handle entrypoint)", justified_by=[cite])
        bid = self.model.beliefs.all()[0].id

        _write(self.root, "api.py", API + "\n\ndef extra():\n    return 2\n")
        self.model.on_edit("api.py")
        # new symbol is in the substrate
        self.assertTrue(any(n.name == "extra" for n in self.model.store.nodes_in_file("api.py")))
        # `handle` itself didn't change, so its citing belief stays fresh
        self.assertFalse(self.model.beliefs.get(bid).stale)

        # now change the cited symbol's own body — that must invalidate it
        _write(self.root, "api.py", API.replace(
            "    return RefundService().refund()",
            "    return RefundService().refund() or None"))
        self.model.on_edit("api.py")
        self.assertTrue(self.model.beliefs.get(bid).stale)

    def test_on_edit_never_raises_on_missing_file(self):
        self.model.build()
        # a path that doesn't exist on disk must not blow up the edit path
        try:
            self.model.on_edit("does/not/exist.py")
        except Exception as e:  # pragma: no cover - failure path
            self.fail(f"on_edit raised: {e!r}")

    def test_on_edit_never_raises_when_indexer_breaks(self):
        self.model.build()

        def boom(*a, **k):
            raise RuntimeError("indexer exploded")

        self.model.indexer.index_file = boom  # type: ignore[assignment]
        try:
            self.model.on_edit("api.py")
        except Exception as e:  # pragma: no cover - failure path
            self.fail(f"on_edit propagated an exception: {e!r}")


# --------------------------------------------------------------------------
# teardown — drop substrate, keep the inferred layer
# --------------------------------------------------------------------------
class TestTeardown(ServiceTestBase):
    def test_teardown_drops_substrate_keeps_inferred(self):
        self.model.build()
        self.model.record_belief("(owns billing refunds)", confidence=0.8)
        self.model.record_invariant(
            "plugins must not call Renderer",
            check="(forbidden-edge :kind calls :from plugins :to Renderer)",
            enforcement=Enforcement.HARD)
        self.model.record_decision(reason="no global Cache singleton",
                                   detector="(construct Cache)")

        self.assertGreater(self.model.store.count_nodes(), 0)
        self.assertTrue(self.model.store.all_files())

        self.model.teardown_substrate()

        # substrate is gone...
        self.assertEqual(self.model.store.count_nodes(), 0)
        self.assertEqual(self.model.store.all_edges(), [])
        self.assertEqual(self.model.store.all_files(), [])
        # ...the inferred layer survives
        self.assertEqual(self.model.store.count_beliefs(), 1)
        self.assertEqual(len(self.model.store.all_invariants()), 1)
        self.assertEqual(len(self.model.store.all_decisions()), 1)


# --------------------------------------------------------------------------
# get_model — one instance per resolved root
# --------------------------------------------------------------------------
class TestGetModelCache(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.orig_cfg = service.CONFIG_DIR
        self.orig_models = dict(service._MODELS)
        service.CONFIG_DIR = Path(self.tmp) / "cfg"
        service._MODELS.clear()

    def tearDown(self):
        for m in service._MODELS.values():
            try:
                m.close()
            except Exception:
                pass
        service._MODELS.clear()
        service._MODELS.update(self.orig_models)
        service.CONFIG_DIR = self.orig_cfg

    def test_same_root_returns_cached_instance(self):
        d = Path(self.tmp) / "a"
        d.mkdir()
        m1 = get_model(d)
        m2 = get_model(d)
        self.assertIs(m1, m2)

    def test_distinct_roots_get_distinct_instances(self):
        a = Path(self.tmp) / "a"
        b = Path(self.tmp) / "b"
        a.mkdir()
        b.mkdir()
        self.assertIsNot(get_model(a), get_model(b))


# --------------------------------------------------------------------------
# read/record delegates
# --------------------------------------------------------------------------
class TestDelegates(ServiceTestBase):
    def setUp(self):
        super().setUp()
        self.model.build()

    def test_affects_finds_transitive_callers(self):
        # refund <- handle <- outer (transitive blast radius)
        res = self.model.affects("refund")
        names = {self.model.store.get_node(s).name for s in res["affected_symbols"]
                 if self.model.store.get_node(s)}
        self.assertIn("handle", names)
        self.assertIn("outer", names)            # transitive
        self.assertIn("api.py", res["affected_files"])

    def test_owns_recalls_descriptive_belief_and_touches_lru(self):
        self.model.record_belief("(owns billing refunds)", confidence=0.8,
                                 kind=BeliefKind.DESCRIPTIVE)
        before = self.model.beliefs.all()[0].last_consulted
        res = self.model.owns("refunds")
        self.assertTrue(res["beliefs"])
        self.assertEqual(res["beliefs"][0]["claim"], "(owns billing refunds)")
        # consult touched the LRU clock
        after = self.model.beliefs.all()[0].last_consulted
        self.assertGreaterEqual(after, before)

    def test_allowed_blocks_on_hard_compiled_invariant(self):
        self.model.record_invariant(
            "plugins must not call Renderer",
            check="(forbidden-edge :kind calls :from plugins :to Renderer)",
            enforcement=Enforcement.HARD)
        res = self.model.allowed("plugins calling Renderer",
                                 changed_files=["plugins/widget.py"])
        self.assertFalse(res["allowed"])
        self.assertTrue(res["blocking"])

    def test_architecture_index_and_answer_render_text(self):
        idx = self.model.architecture_index()
        self.assertIn("(architecture", idx)
        self.assertIn("(modules", idx)
        ans = self.model.answer("what does refund affect?")
        self.assertIn("refund", ans)


# --------------------------------------------------------------------------
# belief tricky points, exercised through the service's managers
# --------------------------------------------------------------------------
class TestBeliefSemantics(ServiceTestBase):
    def setUp(self):
        super().setUp()
        self.model.build()

    def test_supersession_raises_confidence_in_place_and_bumps_confirmations(self):
        b1 = self.model.record_belief("(owns billing refunds)", confidence=0.5)
        self.assertEqual(b1.confirmations, 1)
        b2 = self.model.record_belief("(owns billing refunds)", confidence=0.9)
        # same row, confidence raised toward the better value, no new belief
        self.assertEqual(b2.id, b1.id)
        self.assertEqual(self.model.store.count_beliefs(), 1)
        self.assertEqual(b2.confirmations, 2)
        self.assertGreater(b2.confidence, b1.confidence)
        self.assertEqual(b2.created, b1.created)   # original provenance kept

    def test_demote_below_floor_deletes(self):
        b = self.model.record_belief("(owns billing refunds)", confidence=0.2)
        bid = b.id
        # one demote drops it under the 0.15 floor -> deleted
        gone = self.model.beliefs.demote(bid, amount=0.25)
        self.assertIsNone(gone)
        self.assertIsNone(self.model.beliefs.get(bid))

    def test_invalidate_marks_citing_belief_stale(self):
        cite = format_citation("billing/refunds.py", "RefundService.refund")
        b = self.model.record_belief("(owns billing refunds)", justified_by=[cite])
        self.assertFalse(b.stale)
        ids = self.model.beliefs.invalidate(["billing/refunds.py"])
        self.assertIn(b.id, ids)
        self.assertTrue(self.model.beliefs.get(b.id).stale)

    def test_consult_lazily_reverifies_and_demotes_when_citation_gone(self):
        cite = format_citation("billing/refunds.py", "RefundService.refund")
        b = self.model.record_belief("(owns billing refunds)", confidence=0.8,
                                     justified_by=[cite])
        # delete the cited symbol so the citation no longer resolves
        _write(self.root, "billing/refunds.py",
               "class RefundService:\n    def other(self):\n        return 1\n")
        self.model.on_edit("billing/refunds.py")    # marks the belief stale
        self.assertTrue(self.model.beliefs.get(b.id).stale)
        # consulting a stale belief triggers lazy reverify -> citation gone -> demote
        after = self.model.consult(b.id)
        self.assertIsNotNone(after)
        self.assertLess(after.confidence, 0.8)

    def test_consult_lazily_reinforces_when_citation_still_resolves(self):
        cite = format_citation("billing/refunds.py", "RefundService.refund")
        b = self.model.record_belief("(owns billing refunds)", confidence=0.6,
                                     justified_by=[cite])
        # change the cited symbol's own slice so invalidation marks it stale,
        # but the symbol still exists -> reverify should reinforce.
        _write(self.root, "billing/refunds.py",
               REFUNDS.replace("return self.retry()", "return self.retry() or 0"))
        self.model.on_edit("billing/refunds.py")
        self.assertTrue(self.model.beliefs.get(b.id).stale)
        after = self.model.consult(b.id)
        self.assertIsNotNone(after)
        self.assertFalse(after.stale)               # reinforced -> stale cleared
        self.assertGreater(after.confidence, 0.6)


# --------------------------------------------------------------------------
# invariant tricky points through the service
# --------------------------------------------------------------------------
class TestInvariantSemantics(ServiceTestBase):
    def setUp(self):
        super().setUp()
        self.model.build()

    def test_forbidden_edge_violation_has_src_location(self):
        self.model.record_invariant(
            "plugins must not call Renderer",
            check="(forbidden-edge :kind calls :from plugins :to Renderer)",
            enforcement=Enforcement.HARD)
        vios = self.model.invariants.check_all()
        self.assertTrue(vios)
        loc = vios[0].location
        self.assertTrue(loc.startswith("plugins/widget.py:"),
                        f"unexpected location {loc!r}")
        # the line points at the Renderer() call site (line 5 of WIDGET)
        line = int(loc.rsplit(":", 1)[1])
        self.assertEqual(line, 5)
        self.assertTrue(vios[0].blocking)           # HARD + compiled

    def test_enforcement_is_asymmetric_soft_compiled_does_not_block(self):
        self.model.record_invariant(
            "plugins should avoid Renderer",
            check="(forbidden-edge :kind calls :from plugins :to Renderer)",
            enforcement=Enforcement.SOFT)
        vios = self.model.invariants.check_all()
        self.assertTrue(vios)                        # still found
        self.assertFalse(any(v.blocking for v in vios))   # but not blocking

    def test_uncompiled_invariant_is_skipped(self):
        # no :check => nothing deterministic to run, even at HARD enforcement
        self.model.record_invariant("be tasteful", enforcement=Enforcement.HARD)
        self.assertEqual(self.model.invariants.check_all(), [])

    def test_rejected_decision_detector_fires_on_diff(self):
        # a module-scope construction of Cache is the rejected pattern
        _write(self.root, "cachemod.py",
               "class Cache:\n    pass\n\n\nCACHE = Cache()\n")
        self.model.sync()
        self.model.record_decision(reason="no global Cache singleton",
                                   detector="(construct Cache)")
        vios = self.model.invariants.detect_rejected(["cachemod.py"])
        self.assertTrue(vios)
        self.assertEqual(vios[0].invariant_id,
                         self.model.store.all_decisions()[0].id)


if __name__ == "__main__":
    unittest.main()
