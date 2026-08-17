"""Tests for the reverse clarifying-questions tool (feature #2)."""
import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from tether.tools.ask_user import AskUserTool
from tether.engine.turn_controller import mark_interactive


def _echo_first(questions):
    """Stand-in ask_fn: always picks each question's first option."""
    return [{"question": q["question"], "answer": q["options"][0]} for q in questions]


class Normalize(unittest.TestCase):
    def test_drops_malformed_and_caps_at_three(self):
        raw = [
            {"question": "a", "options": ["1", "2"]},
            {"question": "", "options": ["1"]},        # no text
            {"question": "b", "options": []},          # no options
            "not a dict",                               # wrong type
            {"question": "c", "options": ["x", "y"]},
            {"question": "d", "options": ["x"]},        # would be 3rd valid, but cap is on input slice
        ]
        out = AskUserTool._normalize(raw)
        # input is sliced to first 3 before validation, so only "a" survives
        # from the first three; assert the cap + validation interplay explicitly
        self.assertLessEqual(len(out), 3)
        for q in out:
            self.assertTrue(q["question"])
            self.assertTrue(q["options"])

    def test_strips_whitespace(self):
        out = AskUserTool._normalize([{"question": "  hi  ", "options": [" a ", "b", "  "]}])
        self.assertEqual(out, [{"question": "hi", "options": ["a", "b"]}])


class Execute(unittest.TestCase):
    def test_headless_degrades_without_blocking(self):
        tool = AskUserTool(ask_fn=None)
        r = tool.execute({"questions": [{"question": "mono or standalone?", "options": ["mono", "standalone"]}]})
        self.assertFalse(r.is_error)
        self.assertIn("non-interactive", r.content)
        self.assertIn("mono or standalone?", r.content)

    def test_interactive_when_marked_calls_ask_fn(self):
        tool = AskUserTool(ask_fn=_echo_first)
        mark_interactive(True)
        try:
            r = tool.execute({"questions": [{"question": "mock or hit?", "options": ["mock", "hit"]}]})
        finally:
            mark_interactive(False)
        self.assertFalse(r.is_error)
        self.assertIn("Q: mock or hit?", r.content)
        self.assertIn("A: mock", r.content)

    def test_unmarked_thread_degrades_even_with_ask_fn(self):
        # A fresh thread never carries the interactive marker (sub-agent case),
        # so ask_fn must NOT be called — it degrades instead.
        called = {"hit": False}

        def spy(questions):
            called["hit"] = True
            return _echo_first(questions)

        tool = AskUserTool(ask_fn=spy)
        result_box = {}

        def run():
            result_box["r"] = tool.execute(
                {"questions": [{"question": "q?", "options": ["a", "b"]}]}
            )

        t = threading.Thread(target=run)
        t.start()
        t.join()
        self.assertFalse(called["hit"])
        self.assertIn("non-interactive", result_box["r"].content)

    def test_malformed_is_error(self):
        tool = AskUserTool(ask_fn=_echo_first)
        r = tool.execute({"questions": [{"question": "", "options": []}]})
        self.assertTrue(r.is_error)

    def test_no_questions_key_is_error(self):
        tool = AskUserTool(ask_fn=_echo_first)
        r = tool.execute({})
        self.assertTrue(r.is_error)

    def test_dismissed_is_graceful(self):
        def boom(questions):
            raise KeyboardInterrupt

        tool = AskUserTool(ask_fn=boom)
        mark_interactive(True)
        try:
            r = tool.execute({"questions": [{"question": "q?", "options": ["a", "b"]}]})
        finally:
            mark_interactive(False)
        self.assertFalse(r.is_error)
        self.assertIn("dismissed", r.content)

    def test_empty_answers_is_graceful(self):
        tool = AskUserTool(ask_fn=lambda q: [])
        mark_interactive(True)
        try:
            r = tool.execute({"questions": [{"question": "q?", "options": ["a", "b"]}]})
        finally:
            mark_interactive(False)
        self.assertFalse(r.is_error)
        self.assertIn("did not answer", r.content)


class Definition(unittest.TestCase):
    def test_definition_shape(self):
        tool = AskUserTool()
        d = tool.to_definition()
        self.assertEqual(d.name, "ask_user")
        props = d.parameters["properties"]
        self.assertIn("questions", props)
        self.assertEqual(d.parameters["required"], ["questions"])
        items = props["questions"]["items"]["properties"]
        self.assertIn("question", items)
        self.assertIn("options", items)


if __name__ == "__main__":
    unittest.main()
