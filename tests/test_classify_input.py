"""Tests for classify_input — the pure disposition logic of the live REPL loop."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

try:
    from tether.ui.repl import classify_input, resolve_choice, paste_summary
    _IMPORT_ERR = None
except Exception as e:  # pragma: no cover - prompt_toolkit may be absent
    classify_input = resolve_choice = paste_summary = None
    _IMPORT_ERR = e


@unittest.skipIf(classify_input is None, f"prompt_toolkit unavailable: {_IMPORT_ERR}")
class Classify(unittest.TestCase):
    def test_pending_takes_precedence(self):
        # Even a slash or blank line goes to the waiting request verbatim.
        self.assertEqual(classify_input("/help", busy=True, has_pending=True), ("fulfill", "/help"))
        self.assertEqual(classify_input("y", busy=False, has_pending=True), ("fulfill", "y"))

    def test_blank_is_noop(self):
        self.assertEqual(classify_input("   ", busy=False, has_pending=False), ("noop", None))

    def test_idle_prose_starts_turn(self):
        self.assertEqual(classify_input("build a thing", False, False), ("submit", "build a thing"))

    def test_busy_prose_queues_as_also(self):
        self.assertEqual(classify_input("and add tests", True, False), ("also", "and add tests"))

    def test_also_command(self):
        self.assertEqual(classify_input("/also more", True, False), ("also", "more"))
        self.assertEqual(classify_input("/also   ", True, False), ("noop", None))

    def test_redirect_command(self):
        self.assertEqual(classify_input("/redirect use sqlite", True, False), ("redirect", "use sqlite"))
        self.assertEqual(classify_input("/redirect", True, False), ("noop", None))

    def test_stop_and_interrupt(self):
        self.assertEqual(classify_input("/stop", True, False), ("interrupt", None))
        self.assertEqual(classify_input("/interrupt", True, False), ("interrupt", None))

    def test_other_slash_idle_vs_busy(self):
        self.assertEqual(classify_input("/help", False, False), ("slash", "/help"))
        self.assertEqual(classify_input("/clear", True, False), ("slash_busy", "/clear"))


@unittest.skipIf(resolve_choice is None, f"prompt_toolkit unavailable: {_IMPORT_ERR}")
class ResolveChoice(unittest.TestCase):
    def test_number_selects_option(self):
        self.assertEqual(resolve_choice("2", ["a", "b", "c"]), "b")

    def test_empty_takes_first(self):
        self.assertEqual(resolve_choice("", ["a", "b"]), "a")

    def test_out_of_range_kept_verbatim(self):
        self.assertEqual(resolve_choice("9", ["a", "b"]), "9")

    def test_custom_answer(self):
        self.assertEqual(resolve_choice("mongo", ["postgres", "sqlite"]), "mongo")


@unittest.skipIf(paste_summary is None, f"prompt_toolkit unavailable: {_IMPORT_ERR}")
class PasteSummary(unittest.TestCase):
    def test_short_input_no_summary(self):
        self.assertIsNone(paste_summary("one\ntwo\nthree"))

    def test_long_paste_summarized(self):
        out = paste_summary("\n".join(f"line {i}" for i in range(10)))
        self.assertIsNotNone(out)
        self.assertIn("pasted 10 lines", out)
        self.assertIn("line 0", out)


if __name__ == "__main__":
    unittest.main()
