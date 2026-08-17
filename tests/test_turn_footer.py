"""Tests for the per-turn footer (elapsed · tokens · tools · low-confidence)."""
import contextlib
import io
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

try:
    from tether.ui.repl import TetherREPL
    from tether.core.config import TetherConfig
    from tether.core.models import UsageSummary
    _IMPORT_ERR = None
except Exception as e:  # pragma: no cover - prompt_toolkit may be absent
    TetherREPL = None
    _IMPORT_ERR = e


class _Result:
    def __init__(self, tools, stop="completed"):
        self.tool_calls_made = tools
        self.stop_reason = stop


@unittest.skipIf(TetherREPL is None, f"prompt_toolkit unavailable: {_IMPORT_ERR}")
class Footer(unittest.TestCase):
    def _repl(self):
        repl = TetherREPL(TetherConfig(provider="local"))
        # minimal engine stand-in
        class Eng:
            usage = UsageSummary(input_tokens=900, output_tokens=100)  # total 1000
            _turn_low_confidence = []
        repl._engine = Eng()
        return repl

    def _footer(self, repl, result, elapsed):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            repl._on_turn_complete(result, elapsed)
        return buf.getvalue()

    def test_basic_footer(self):
        repl = self._repl()
        out = self._footer(repl, _Result(["bash", "file_edit"]), 4.2)
        self.assertIn("4.2s", out)
        self.assertIn("1.0k tok", out)
        self.assertIn("2 tools", out)

    def test_token_delta_between_turns(self):
        repl = self._repl()
        self._footer(repl, _Result([]), 1.0)          # consumes 1000
        repl._engine.usage = type(repl._engine.usage)(input_tokens=1400, output_tokens=100)  # total 1500
        out = self._footer(repl, _Result([]), 1.0)
        self.assertIn("500 tok", out)                  # delta, not cumulative

    def test_low_confidence_callout(self):
        repl = self._repl()
        repl._engine._turn_low_confidence = [("auth.py", 0.30), ("db.py", 0.40)]
        out = self._footer(repl, _Result(["file_edit"]), 2.0)
        self.assertIn("2 low-confidence edits", out)
        self.assertIn("auth.py (0.30)", out)
        self.assertIn("/why", out)

    def test_error_stop_reason_shown(self):
        repl = self._repl()
        out = self._footer(repl, _Result([], stop="max_turns_reached"), 1.0)
        self.assertIn("max_turns_reached", out)


if __name__ == "__main__":
    unittest.main()
