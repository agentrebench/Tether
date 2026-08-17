"""Behavioural tests for the agent-facing model tools (tools/codebase_model_tool.py).

Builds a tiny real fixture repo on disk, indexes it with the real substrate
parser via a real CodebaseModel (temp db), seeds beliefs/invariants/decisions,
and drives the three tools (model_query / model_record / model_check) the way the
engine would — through ``execute(arguments)`` — asserting on the rendered text,
the error flags, and the graceful empty-model degradation.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from tether.core.codebase_model.service import CodebaseModel
from tether.tools.codebase_model_tool import (
    ModelCheckTool, ModelQueryTool, ModelRecordTool,
)


# billing: refund() -> retry(); api.handle -> refund; api.outer -> handle.
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

# a module-scope construction of Cache — the rejected singleton pattern.
CACHEMOD = """\
class Cache:
    pass


CACHE = Cache()
"""


class _ToolTestBase(unittest.TestCase):
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
        self.db = os.path.join(tempfile.mkdtemp(), "model.db")
        self.model = CodebaseModel(self.dir, db_path=self.db)
        self.model.build()
        self.query = ModelQueryTool(model=self.model)
        self.record = ModelRecordTool(model=self.model)
        self.check = ModelCheckTool(model=self.model)

    def tearDown(self):
        self.model.close()

    def _seed_rules(self):
        # forbidden plugins -> Renderer, HARD + compiled => may hard-block.
        self.model.record_invariant(
            "plugins must not call Renderer",
            check="(forbidden-edge :kind calls :from plugins :to Renderer)",
            enforcement="hard", confidence="confirmed")
        # ownership belief, cited.
        self.model.record_belief(
            "(owns billing refunds)",
            justified_by=["billing/refunds.py @ RefundService.refund"])
        # rejected singleton construction of Cache.
        self.model.record_decision(
            reason="no module-scope singletons",
            detector="(construct Cache)", accepted_pattern="inject the cache")


# --------------------------------------------------------------------------
# model_query
# --------------------------------------------------------------------------
class ModelQueryToolTest(_ToolTestBase):
    def test_architecture_lists_modules(self):
        res = self.query.execute({"action": "architecture"})
        self.assertFalse(res.is_error)
        self.assertIn("(architecture", res.content)
        self.assertIn("billing", res.content)

    def test_affects_finds_transitive_callers(self):
        # editing retry() reaches refund(), handle(), outer() up the call graph.
        res = self.query.execute({"action": "affects", "target": "retry"})
        self.assertFalse(res.is_error)
        self.assertIn("affects", res.content)
        self.assertIn("refund", res.content)
        self.assertIn("billing/api.py", res.content)

    def test_owns_recalls_seeded_belief(self):
        self._seed_rules()
        res = self.query.execute({"action": "owns", "target": "refunds"})
        self.assertFalse(res.is_error)
        self.assertIn("owns billing refunds", res.content)

    def test_owns_no_belief_is_graceful(self):
        # with rules seeded (so the model isn't "empty") but no matching belief.
        self._seed_rules()
        res = self.query.execute({"action": "owns", "target": "telemetry"})
        self.assertFalse(res.is_error)
        self.assertIn("No retained belief", res.content)

    def test_allowed_flags_blocking_violation(self):
        self._seed_rules()
        res = self.query.execute({"action": "allowed",
                                  "target": "let plugins call Renderer"})
        self.assertFalse(res.is_error)
        self.assertIn("Not allowed", res.content)
        self.assertIn("BLOCKING", res.content)
        self.assertIn("plugins/widget.py", res.content)

    def test_unknown_action_errors(self):
        res = self.query.execute({"action": "frobnicate"})
        self.assertTrue(res.is_error)
        self.assertIn("Unknown action", res.content)

    def test_affects_requires_target(self):
        res = self.query.execute({"action": "affects"})
        self.assertTrue(res.is_error)
        self.assertIn("requires a `target`", res.content)

    def test_empty_model_degrades_gracefully(self):
        empty_db = os.path.join(tempfile.mkdtemp(), "empty.db")
        empty = CodebaseModel(tempfile.mkdtemp(), db_path=empty_db)
        try:
            tool = ModelQueryTool(model=empty)
            res = tool.execute({"action": "owns", "target": "anything"})
            self.assertFalse(res.is_error)
            self.assertIn("empty", res.content.lower())
        finally:
            empty.close()


# --------------------------------------------------------------------------
# model_record
# --------------------------------------------------------------------------
class ModelRecordToolTest(_ToolTestBase):
    def test_record_belief_then_query_recalls_it(self):
        res = self.record.execute({
            "kind": "belief",
            "claim": "(owns render drawing)",
            "citations": ["render/engine.py @ Renderer.draw"],
            "confidence": 0.8,
        })
        self.assertFalse(res.is_error)
        self.assertIn("Recorded belief", res.content)
        # round-trip: the query tool can now recall it.
        got = self.query.execute({"action": "owns", "target": "drawing"})
        self.assertIn("owns render drawing", got.content)

    def test_record_belief_supersession_bumps_confirmations(self):
        first = self.record.execute({"kind": "belief", "claim": "(owns billing refunds)"})
        self.assertIn("confirmations 1", first.content)
        again = self.record.execute({"kind": "belief", "claim": "(owns billing refunds)"})
        self.assertIn("confirmations 2", again.content)

    def test_record_belief_requires_claim(self):
        res = self.record.execute({"kind": "belief"})
        self.assertTrue(res.is_error)
        self.assertIn("requires a `claim`", res.content)

    def test_record_invariant_compiled_vs_soft(self):
        compiled = self.record.execute({
            "kind": "invariant",
            "claim": "plugins must not call Renderer",
            "check": "(forbidden-edge :kind calls :from plugins :to Renderer)",
        })
        self.assertFalse(compiled.is_error)
        self.assertIn("compiled-and-checkable", compiled.content)
        soft = self.record.execute({"kind": "invariant", "claim": "keep it simple"})
        self.assertIn("soft", soft.content)

    def test_record_decision_with_detector(self):
        res = self.record.execute({
            "kind": "decision",
            "reason": "no module-scope singletons",
            "detector": "(construct Cache)",
            "accepted_pattern": "inject the cache",
        })
        self.assertFalse(res.is_error)
        self.assertIn("Recorded decision", res.content)
        self.assertIn("compiled detector", res.content)

    def test_record_decision_requires_reason(self):
        res = self.record.execute({"kind": "decision", "detector": "(construct Cache)"})
        self.assertTrue(res.is_error)
        self.assertIn("requires a `reason`", res.content)

    def test_unknown_kind_errors(self):
        res = self.record.execute({"kind": "rumor", "claim": "x"})
        self.assertTrue(res.is_error)
        self.assertIn("Unknown kind", res.content)


# --------------------------------------------------------------------------
# model_check
# --------------------------------------------------------------------------
class ModelCheckToolTest(_ToolTestBase):
    def test_check_all_reports_blocking_forbidden_edge(self):
        self._seed_rules()
        res = self.check.execute({})
        self.assertFalse(res.is_error)
        self.assertIn("BLOCKING", res.content)
        self.assertIn("plugins/widget.py", res.content)
        # the s-expr location is path:line.
        self.assertRegex(res.content, r"plugins/widget\.py:\d+")

    def test_check_diff_detects_rejected_pattern(self):
        self._seed_rules()
        # restrict the diff to the singleton module; the forbidden-edge file is
        # excluded, so only the rejected-pattern detector should fire (soft).
        res = self.check.execute({"changed_files": ["cache/glob.py"]})
        self.assertFalse(res.is_error)
        self.assertIn("no module-scope singletons", res.content)
        self.assertIn("cache/glob.py", res.content)
        self.assertNotIn("plugins/widget.py", res.content)

    def test_check_diff_unrelated_file_no_violations(self):
        self._seed_rules()
        res = self.check.execute({"changed_files": ["billing/refunds.py"]})
        self.assertFalse(res.is_error)
        self.assertIn("No violations", res.content)

    def test_check_with_no_rules_is_graceful(self):
        # no invariants/decisions recorded on this fresh-built model.
        res = self.check.execute({})
        self.assertFalse(res.is_error)
        self.assertIn("No invariants or decisions", res.content)


if __name__ == "__main__":
    unittest.main()
