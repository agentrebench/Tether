"""Tests for confidence-shaded diffs (feature #3)."""
import contextlib
import io
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

import tether.ui.colors as colors
from tether.ui.colors import (
    confidence_rgb, confidence_color, confidence_label, confidence_bar,
)
from tether.tools.file_edit import FileEditTool
from tether.core.config import TetherConfig
from tether.core.permissions import PermissionContext
from tether.engine.query_engine import QueryEngine
from tether.tools.base import ToolRegistry


class ColorHelpers(unittest.TestCase):
    def test_rgb_endpoints_and_midpoint(self):
        # low -> reddish, high -> greenish
        lr, lg, lb = confidence_rgb(0.0)
        hr, hg, hb = confidence_rgb(1.0)
        self.assertGreater(lr, lg)   # red dominant at low
        self.assertGreater(hg, hr)   # green dominant at high

    def test_color_is_truecolor_escape(self):
        prev = colors.COLOR_ENABLED
        prev_truecolor = colors.TRUECOLOR
        colors.COLOR_ENABLED = True
        colors.TRUECOLOR = True
        try:
            esc = confidence_color(0.5)
        finally:
            colors.COLOR_ENABLED = prev
            colors.TRUECOLOR = prev_truecolor
        self.assertTrue(esc.startswith("\033[38;2;"))
        self.assertTrue(esc.endswith("m"))

    def test_color_disabled_returns_empty(self):
        prev = colors.COLOR_ENABLED
        colors.COLOR_ENABLED = False
        try:
            self.assertEqual(confidence_color(0.5), "")
        finally:
            colors.COLOR_ENABLED = prev

    def test_label_thresholds(self):
        self.assertEqual(confidence_label(0.95), "high")
        self.assertEqual(confidence_label(0.8), "high")
        self.assertEqual(confidence_label(0.6), "medium")
        self.assertEqual(confidence_label(0.5), "medium")
        self.assertEqual(confidence_label(0.49), "low")
        self.assertEqual(confidence_label(0.0), "low")

    def test_bar_fill_and_clamp(self):
        self.assertEqual(confidence_bar(1.0, width=10), "█" * 10)
        self.assertEqual(confidence_bar(0.0, width=10), "░" * 10)
        self.assertEqual(len(confidence_bar(0.37, width=10)), 10)
        # out-of-range inputs clamp rather than overflow
        self.assertEqual(confidence_bar(5.0, width=8), "█" * 8)
        self.assertEqual(confidence_bar(-3.0, width=8), "░" * 8)


class FileEditEmitsConfidence(unittest.TestCase):
    def _edit(self, **extra):
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
            f.write("alpha\nbeta\ngamma\n")
            path = f.name
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        args = {"file_path": path, "old_string": "beta", "new_string": "BETA"}
        args.update(extra)
        return FileEditTool().execute(args)

    def test_confidence_marker_emitted(self):
        r = self._edit(confidence=0.4)
        self.assertFalse(r.is_error)
        self.assertIn("CONFIDENCE: 0.40", r.content)

    def test_confidence_omitted_when_absent(self):
        r = self._edit()
        self.assertNotIn("CONFIDENCE:", r.content)

    def test_confidence_clamped(self):
        self.assertIn("CONFIDENCE: 1.00", self._edit(confidence=2.5).content)
        self.assertIn("CONFIDENCE: 0.00", self._edit(confidence=-1).content)

    def test_bool_confidence_ignored(self):
        # True is an int subclass; must not be treated as a numeric confidence
        r = self._edit(confidence=True)
        self.assertNotIn("CONFIDENCE:", r.content)


def _engine():
    cfg = TetherConfig(provider="local")
    return QueryEngine(cfg, ToolRegistry.build_default(), PermissionContext.from_config(cfg))


class RendererShading(unittest.TestCase):
    DIFF = (
        "SUMMARY: edited x.py via replace_string — swapped beta\n"
        "{conf}"
        "--- x.py (before)\n"
        "+++ x.py (after)\n"
        "@@ -1,3 +1,3 @@\n"
        "-beta\n"
        "+BETA\n"
        "(Do not re-read this file to verify.)"
    )

    def _render(self, conf_line):
        eng = _engine()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            eng._render_file_edit_result(self.DIFF.format(conf=conf_line))
        return buf.getvalue()

    def test_marker_consumed_and_banner_shown(self):
        out = self._render("CONFIDENCE: 0.40\n")
        # raw marker must not be printed
        self.assertNotIn("CONFIDENCE: 0.40", out)
        # a shaded banner is shown instead
        self.assertIn("confidence 0.40", out)
        self.assertIn("low", out)
        # gutter rail present on body lines
        self.assertIn("│", out)
        # diff content still rendered
        self.assertIn("BETA", out)

    def test_no_confidence_is_backward_compatible(self):
        out = self._render("")
        self.assertNotIn("confidence ", out)   # no banner
        self.assertNotIn("│", out)             # no gutter rail
        self.assertIn("SUMMARY:", out)
        self.assertIn("BETA", out)

    def test_high_confidence_uses_high_label(self):
        out = self._render("CONFIDENCE: 0.95\n")
        self.assertIn("confidence 0.95", out)
        self.assertIn("high", out)

    def test_low_confidence_edit_is_captured_for_footer(self):
        eng = _engine()
        content = (
            "SUMMARY: edited auth.py via exact_replace — swapped check\n"
            "CONFIDENCE: 0.30\n"
            "@@ -1,1 +1,1 @@\n-a\n+b\n(Do not re-read this file to verify.)"
        )
        with contextlib.redirect_stdout(io.StringIO()):
            eng._render_file_edit_result(content)
        self.assertEqual(eng._turn_low_confidence, [("auth.py", 0.30)])

    def test_high_confidence_edit_not_captured(self):
        eng = _engine()
        content = (
            "SUMMARY: edited auth.py via exact_replace — swapped check\n"
            "CONFIDENCE: 0.90\n"
            "@@ -1,1 +1,1 @@\n-a\n+b\n(Do not re-read this file to verify.)"
        )
        with contextlib.redirect_stdout(io.StringIO()):
            eng._render_file_edit_result(content)
        self.assertEqual(eng._turn_low_confidence, [])


if __name__ == "__main__":
    unittest.main()
