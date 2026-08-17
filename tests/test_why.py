"""Tests for per-line "why" capture and lookup (feature #4)."""
import contextlib
import io
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from tether.session.rationale import compute_entries, RationaleStore
from tether.tools.file_edit import FileEditTool
from tether.core.config import TetherConfig
from tether.core.permissions import PermissionContext
from tether.engine.query_engine import QueryEngine
from tether.tools.base import ToolRegistry


class ComputeEntries(unittest.TestCase):
    def test_matches_added_line_by_substring(self):
        original = "a\nb\nc\n"
        updated = "a\nb\n    result = compute(x)\nc\n"
        out = compute_entries(original, updated, {"compute(x)": "x is pre-validated"})
        self.assertEqual(out, [{"line": 3, "code": "result = compute(x)", "rationale": "x is pre-validated"}])

    def test_no_rationale_returns_empty(self):
        self.assertEqual(compute_entries("a\n", "a\nb\n", None), [])
        self.assertEqual(compute_entries("a\n", "a\nb\n", {}), [])
        self.assertEqual(compute_entries("a\n", "a\nb\n", "nope"), [])

    def test_skips_short_keys_and_empty_rationale(self):
        original = "a\n"
        updated = "a\nxx_long_line_here\n"
        self.assertEqual(compute_entries(original, updated, {"x": "too short a key"}), [])
        self.assertEqual(compute_entries(original, updated, {"long_line": "   "}), [])

    def test_each_key_annotates_first_match_only(self):
        original = "a\n"
        updated = "a\nfoo()\nfoo()\n"
        out = compute_entries(original, updated, {"foo()": "the call"})
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["line"], 2)

    def test_replace_mode_line_numbers(self):
        original = "one\ntwo\nthree\n"
        updated = "one\nTWO_CHANGED\nthree\n"
        out = compute_entries(original, updated, {"TWO_CHANGED": "renamed for clarity"})
        self.assertEqual(out[0]["line"], 2)


class Store(unittest.TestCase):
    def _file(self, text):
        f = tempfile.NamedTemporaryFile("w", suffix=".py", delete=False)
        f.write(text)
        f.close()
        self.addCleanup(lambda: os.path.exists(f.name) and os.unlink(f.name))
        return f.name

    def test_record_and_exact_lookup(self):
        path = self._file("a\nb\nkeyline\nd\n")
        store = RationaleStore()
        store.record(path, [{"line": 3, "code": "keyline", "rationale": "because reasons"}])
        info = store.lookup(path, 3)
        self.assertIsNotNone(info)
        self.assertEqual(info["rationale"], "because reasons")
        self.assertFalse(info["moved"])

    def test_lookup_unknown_returns_none(self):
        path = self._file("a\nb\n")
        store = RationaleStore()
        self.assertIsNone(store.lookup(path, 1))

    def test_lookup_relocates_when_line_shifted(self):
        path = self._file("a\nb\nkeyline\nd\n")
        store = RationaleStore()
        store.record(path, [{"line": 3, "code": "keyline", "rationale": "moved rationale"}])
        # Prepend two lines so keyline is now at line 5.
        with open(path, "w") as fh:
            fh.write("x\ny\na\nb\nkeyline\nd\n")
        info = store.lookup(path, 3)
        self.assertTrue(info["moved"])
        self.assertEqual(info["actual_line"], 5)
        self.assertEqual(info["rationale"], "moved rationale")

    def test_lookup_by_content_when_asking_new_line(self):
        path = self._file("a\nb\nkeyline\nd\n")
        store = RationaleStore()
        store.record(path, [{"line": 3, "code": "keyline", "rationale": "content match"}])
        with open(path, "w") as fh:
            fh.write("x\ny\na\nb\nkeyline\nd\n")
        # Ask about line 5 (where keyline now lives) — no bucket entry there,
        # resolved by content.
        info = store.lookup(path, 5)
        self.assertIsNotNone(info)
        self.assertEqual(info["rationale"], "content match")


class FileEditIntegration(unittest.TestCase):
    def _file(self):
        f = tempfile.NamedTemporaryFile("w", suffix=".py", delete=False)
        f.write("alpha\nbeta\ngamma\n")
        f.close()
        self.addCleanup(lambda: os.path.exists(f.name) and os.unlink(f.name))
        return f.name

    def test_records_and_emits_marker(self):
        path = self._file()
        store = RationaleStore()
        tool = FileEditTool(rationale_store=store)
        r = tool.execute({
            "file_path": path,
            "old_string": "beta",
            "new_string": "BETA_X",
            "line_rationale": {"BETA_X": "uppercased to match the new enum"},
        })
        self.assertFalse(r.is_error)
        self.assertIn(f"WHY_LINES: {path}|2", r.content)
        info = store.lookup(path, 2)
        self.assertEqual(info["rationale"], "uppercased to match the new enum")

    def test_no_rationale_no_marker(self):
        path = self._file()
        store = RationaleStore()
        tool = FileEditTool(rationale_store=store)
        r = tool.execute({"file_path": path, "old_string": "beta", "new_string": "BETA_X"})
        self.assertNotIn("WHY_LINES:", r.content)
        self.assertEqual(store.entries_for(path), {})

    def test_works_without_store(self):
        # Headless / sub-agent: no store injected, edit still succeeds.
        path = self._file()
        tool = FileEditTool()
        r = tool.execute({
            "file_path": path, "old_string": "beta", "new_string": "BETA_X",
            "line_rationale": {"BETA_X": "why"},
        })
        self.assertFalse(r.is_error)
        self.assertIn("WHY_LINES:", r.content)  # marker still emitted for the hint


def _engine():
    cfg = TetherConfig(provider="local")
    return QueryEngine(cfg, ToolRegistry.build_default(), PermissionContext.from_config(cfg))


class RendererHint(unittest.TestCase):
    def test_marker_consumed_and_hint_shown(self):
        content = (
            "SUMMARY: edited x.py via exact_replace — replaced 1\n"
            "WHY_LINES: x.py|2,5\n"
            "@@ -1,3 +1,3 @@\n-beta\n+BETA\n"
            "(Do not re-read this file to verify.)"
        )
        eng = _engine()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            eng._render_file_edit_result(content)
        out = buf.getvalue()
        self.assertNotIn("WHY_LINES:", out)          # raw marker consumed
        self.assertIn("2 annotated lines", out)        # hint shown
        self.assertIn("/why x.py:<line>", out)


if __name__ == "__main__":
    unittest.main()
