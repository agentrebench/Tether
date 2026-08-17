"""Tests for the terminal renderer that collects clarifying-question answers
(ui.repl.ask_clarifying_questions) — the interactive half of feature #2."""
import builtins
import io
import contextlib
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

try:
    # ui.repl imports prompt_toolkit at module load; it's a runtime dependency
    # of the app but may be absent in a bare interpreter. Skip rather than fail.
    from tether.ui.repl import ask_clarifying_questions
    _IMPORT_ERR = None
except Exception as e:  # pragma: no cover - environment-dependent
    ask_clarifying_questions = None
    _IMPORT_ERR = e


def _run_with_inputs(questions, inputs):
    """Drive ask_clarifying_questions with a scripted sequence of stdin lines."""
    it = iter(inputs)
    real_input = builtins.input
    builtins.input = lambda *a, **k: next(it)
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            return ask_clarifying_questions(questions)
    finally:
        builtins.input = real_input


@unittest.skipIf(ask_clarifying_questions is None, f"prompt_toolkit unavailable: {_IMPORT_ERR}")
class ClarifyRenderer(unittest.TestCase):
    def test_numeric_selection_maps_to_option(self):
        qs = [{"question": "lang?", "options": ["TypeScript", "JavaScript"]}]
        out = _run_with_inputs(qs, ["2"])
        self.assertEqual(out, [{"question": "lang?", "answer": "JavaScript"}])

    def test_custom_free_form_answer_preserved(self):
        qs = [{"question": "which db?", "options": ["postgres", "sqlite"]}]
        out = _run_with_inputs(qs, ["mongo"])
        self.assertEqual(out, [{"question": "which db?", "answer": "mongo"}])

    def test_empty_defaults_to_first_option(self):
        qs = [{"question": "monorepo?", "options": ["yes", "no"]}]
        out = _run_with_inputs(qs, [""])
        self.assertEqual(out, [{"question": "monorepo?", "answer": "yes"}])

    def test_out_of_range_number_kept_verbatim(self):
        qs = [{"question": "pick", "options": ["a", "b"]}]
        out = _run_with_inputs(qs, ["9"])
        self.assertEqual(out, [{"question": "pick", "answer": "9"}])

    def test_multiple_questions(self):
        qs = [
            {"question": "q1", "options": ["a", "b"]},
            {"question": "q2", "options": ["c", "d"]},
        ]
        out = _run_with_inputs(qs, ["1", "d"])
        self.assertEqual(
            out,
            [{"question": "q1", "answer": "a"}, {"question": "q2", "answer": "d"}],
        )


if __name__ == "__main__":
    unittest.main()
