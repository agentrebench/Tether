"""Behavioural tests for the invariant compiler (core/codebase_model/invariants.py).

Exercises real compilation + evaluation against substrate produced by the real
parser, so violation locations are genuine path:line slices.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from tether.core.codebase_model import substrate
from tether.core.codebase_model.invariants import InvariantEngine, slugify
from tether.core.codebase_model.model import Enforcement
from tether.core.codebase_model.store import ModelStore


# A plugin that calls Renderer from inside a function (forbidden cross-module edge).
WIDGET = """\
from render.engine import Renderer


def show():
    r = Renderer()
    r.draw()
"""

# A plugin that constructs Renderer at module/global scope.
GLOBALS = """\
from render.engine import Renderer

CACHE = Renderer()
"""

# The render module that legitimately owns Renderer.
ENGINE = """\
class Renderer:
    def draw(self):
        return 1
"""


def _line_of(content: str, needle: str) -> int:
    for i, line in enumerate(content.splitlines(), start=1):
        if needle in line:
            return i
    raise AssertionError(f"{needle!r} not in content")


class InvariantEngineTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.store = ModelStore(os.path.join(self.dir, "model.db"))
        self.files = {
            "plugins/widget.py": WIDGET,
            "plugins/globals.py": GLOBALS,
            "render/engine.py": ENGINE,
        }
        for path, content in self.files.items():
            nodes, edges = substrate.extract(path, content)
            self.store.upsert_nodes(nodes)
            self.store.insert_edges(edges)
        self.eng = InvariantEngine(self.store)

    def tearDown(self):
        self.store.close()

    # -- capture -------------------------------------------------------
    def test_add_persists_with_slug_id(self):
        inv = self.eng.add("plugins must not call Renderer",
                            check='(forbidden-edge :kind calls :from plugins :to Renderer)',
                            enforcement=Enforcement.HARD, confidence="confirmed")
        self.assertEqual(inv.id, "plugins-must-not-call-renderer")
        stored = self.store.get_invariant(inv.id)
        self.assertIsNotNone(stored)
        self.assertTrue(stored.compiled)
        self.assertEqual(stored.enforcement, Enforcement.HARD)

    def test_add_decision_persists(self):
        dec = self.eng.add_decision(reason="singleton cache caused stale state",
                                    detector="(construct Renderer)",
                                    accepted_pattern="scoped-provider")
        self.assertEqual(dec.id, slugify("singleton cache caused stale state"))
        self.assertEqual(self.store.get_decision(dec.id).detector, "(construct Renderer)")

    # -- compilation ---------------------------------------------------
    def test_compile_check_unknown_head_returns_none(self):
        self.assertIsNone(self.eng.compile_check("(no-such-check :foo bar)"))

    def test_compile_check_bad_parse_returns_none(self):
        self.assertIsNone(self.eng.compile_check("(forbidden-edge :kind"))  # unbalanced
        self.assertIsNone(self.eng.compile_check(""))

    def test_compile_check_evaluator_finds_edge(self):
        ev = self.eng.compile_check('(forbidden-edge :kind calls :from plugins :to Renderer)')
        self.assertIsNotNone(ev)
        vios = ev(self.store.all_edges())
        # widget.show() and globals (module scope) both call Renderer
        locs = sorted(v.location for v in vios)
        self.assertIn(f"plugins/widget.py:{_line_of(WIDGET, 'Renderer()')}", locs)
        self.assertIn(f"plugins/globals.py:{_line_of(GLOBALS, 'Renderer()')}", locs)

    # -- forbidden-edge location ---------------------------------------
    def test_forbidden_edge_location_is_real_path_line(self):
        inv = self.eng.add("plugins must not call Renderer",
                            check='(forbidden-edge :kind calls :from plugins :to Renderer)',
                            enforcement=Enforcement.HARD, confidence="confirmed")
        vios = self.eng.run(inv)
        widget_vio = [v for v in vios if v.location.startswith("plugins/widget.py")]
        self.assertEqual(len(widget_vio), 1)
        self.assertEqual(widget_vio[0].location,
                         f"plugins/widget.py:{_line_of(WIDGET, 'Renderer()')}")
        self.assertEqual(widget_vio[0].invariant_id, inv.id)

    def test_forbidden_edge_no_false_positive(self):
        # The render module itself defines Renderer; :from plugins must not flag it.
        inv = self.eng.add("plugins must not call Renderer",
                            check='(forbidden-edge :kind calls :from plugins :to Renderer)')
        vios = self.eng.run(inv)
        self.assertTrue(all(v.location.startswith("plugins/") for v in vios))

    # -- asymmetric enforcement ----------------------------------------
    def test_hard_only_when_may_hard_block(self):
        check = '(forbidden-edge :kind calls :from plugins :to Renderer)'
        # compiled + HARD => may hard-block => blocking violations
        hard = self.eng.add("hard rule", check=check,
                            enforcement=Enforcement.HARD, confidence="confirmed",
                            inv_id="hard")
        self.assertTrue(hard.may_hard_block())
        self.assertTrue(all(v.blocking for v in self.eng.run(hard)))

        # compiled but SOFT => warn only, never blocking
        soft = self.eng.add("soft rule", check=check,
                            enforcement=Enforcement.SOFT, confidence=0.7, inv_id="soft")
        soft_vios = self.eng.run(soft)
        self.assertTrue(soft_vios)
        self.assertFalse(any(v.blocking for v in soft_vios))

    def test_uncompiled_invariant_run_is_empty(self):
        # An inferred semantic invariant (no check) has nothing to run deterministically.
        inv = self.eng.add("retries must be idempotent", check="",
                            enforcement=Enforcement.SOFT, confidence=0.7)
        self.assertFalse(inv.may_hard_block())  # soft + uncompiled => never blocks
        self.assertEqual(self.eng.run(inv), [])

    # -- no-global-construction ----------------------------------------
    def test_no_global_construction_flags_module_scope_only(self):
        inv = self.eng.add("Renderer must not be built globally",
                            check='(no-global-construction :class Renderer)')
        vios = self.eng.run(inv)
        self.assertEqual([v.location for v in vios],
                         [f"plugins/globals.py:{_line_of(GLOBALS, 'Renderer()')}"])

    # -- check_all / check_diff ----------------------------------------
    def test_check_all_records_results_and_counterexample(self):
        failing = self.eng.add("no plugin->Renderer",
                               check='(forbidden-edge :kind calls :from plugins :to Renderer)',
                               enforcement=Enforcement.HARD, confidence="confirmed",
                               inv_id="failing")
        passing = self.eng.add("no plugin->Missing",
                               check='(forbidden-edge :kind calls :from plugins :to Missing)',
                               inv_id="passing")
        soft_semantic = self.eng.add("semantic", check="", inv_id="semantic")

        vios = self.eng.check_all(commit="abc123")
        self.assertTrue(vios)
        self.assertTrue(all(v.invariant_id == "failing" for v in vios))

        fr = self.store.get_invariant_result("failing", "abc123")
        pr = self.store.get_invariant_result("passing", "abc123")
        self.assertFalse(fr["passed"])
        self.assertTrue(fr["counterexample"].startswith("plugins/"))
        self.assertTrue(pr["passed"])
        self.assertEqual(pr["counterexample"], "")

        # uncompiled invariant skipped entirely: no result row recorded
        self.assertIsNone(self.store.get_invariant_result("semantic", "abc123"))

        # counterexample persisted on the failing invariant, cleared on passing
        self.assertTrue(self.store.get_invariant("failing").counterexample.startswith("plugins/"))
        self.assertEqual(self.store.get_invariant("passing").counterexample, "")

    def test_check_diff_scopes_to_changed_files(self):
        self.eng.add("no plugin->Renderer",
                     check='(forbidden-edge :kind calls :from plugins :to Renderer)',
                     enforcement=Enforcement.HARD, confidence="confirmed")
        # Only the function-call site is in the diff; module-scope file is not.
        vios = self.eng.check_diff(["plugins/widget.py"], commit="diff1")
        self.assertEqual([v.location for v in vios],
                         [f"plugins/widget.py:{_line_of(WIDGET, 'Renderer()')}"])
        # globals.py untouched => its violation is not reported
        self.assertFalse(any("globals.py" in v.location for v in vios))

    # -- decision detectors --------------------------------------------
    def test_detect_rejected_construct(self):
        self.eng.add_decision(reason="no global Renderer", detector="(construct Renderer)",
                              dec_id="d1")
        vios = self.eng.detect_rejected(["plugins/globals.py", "plugins/widget.py"])
        # construct only matches the module-scope construction in globals.py
        self.assertEqual([v.location for v in vios],
                         [f"plugins/globals.py:{_line_of(GLOBALS, 'Renderer()')}"])
        self.assertEqual(vios[0].invariant_id, "d1")
        self.assertEqual(vios[0].claim, "no global Renderer")
        self.assertEqual(vios[0].enforcement, Enforcement.SOFT)
        self.assertFalse(vios[0].blocking)

    def test_detect_rejected_call_name_from_pattern(self):
        self.eng.add_decision(reason="plugins may not draw",
                              detector='(call-name draw :from plugins)', dec_id="d2")
        vios = self.eng.detect_rejected(["plugins/widget.py", "render/engine.py"])
        # r.draw() is called from plugins/widget.py::show, not from render/*
        self.assertEqual(len(vios), 1)
        self.assertTrue(vios[0].location.startswith("plugins/widget.py:"))

    def test_detect_rejected_ignores_archived(self):
        dec = self.eng.add_decision(reason="archived rule", detector="(construct Renderer)",
                                    dec_id="d3")
        dec.archived = True
        self.store.put_decision(dec)
        vios = self.eng.detect_rejected(["plugins/globals.py"])
        self.assertEqual(vios, [])

    def test_detect_rejected_only_changed_files(self):
        self.eng.add_decision(reason="no global Renderer", detector="(construct Renderer)",
                              dec_id="d4")
        # globals.py NOT in the diff => no detection
        vios = self.eng.detect_rejected(["plugins/widget.py"])
        self.assertEqual(vios, [])


if __name__ == "__main__":
    unittest.main()
