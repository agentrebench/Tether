"""Regression tests for bugs found in the 2026-08 code review: s-expression
escape round-trips, line-based file edits on files without a trailing newline,
intraword emphasis in the markdown renderer, tool-batch-safe compaction,
local->local provider re-selection, and turn-limit reporting for sub-agents."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from tether.core.codebase_model.sexpr import Symbol, read, write
from tether.core.config import DEFAULT_CONTEXT_SIZE, TetherConfig, apply_provider_selection
from tether.core.models import Message, ToolCall
from tether.engine.query_engine import QueryEngine
from tether.tools.file_edit import FileEditTool
from tether.ui.markdown import to_runs


class SexprRoundTrip(unittest.TestCase):
    def test_backslash_before_escape_letter_round_trips(self):
        for s in ["C:\\new", 'say "hi"\\n', "tab\\there", "plain", "\\\\", "a\nb\tc"]:
            with self.subTest(s=s):
                self.assertEqual(read(write(s)), s)

    def test_inf_nan_are_symbols_not_floats(self):
        forms = read("(inf nan Infinity -3e2 .5 +4 7)")
        self.assertEqual(forms[:3], [Symbol("inf"), Symbol("nan"), Symbol("Infinity")])
        self.assertEqual(forms[3:], [-300.0, 0.5, 4, 7])


class FileEditNoTrailingNewline(unittest.TestCase):
    def setUp(self):
        self.tool = FileEditTool.__new__(FileEditTool)

    def test_replace_middle_line_keeps_following_line_separate(self):
        self.assertEqual(self.tool._replace_lines("a\nb\nc", 2, 2, "X")[0], "a\nX\nc")

    def test_replace_last_line_keeps_no_trailing_newline(self):
        self.assertEqual(self.tool._replace_lines("a\nb\nc", 3, 3, "X")[0], "a\nb\nX")

    def test_insert_before_first_line(self):
        self.assertEqual(self.tool._insert_at_line("a\nb", 1, "c", before=True)[0], "c\na\nb")

    def test_insert_after_last_line_without_newline(self):
        self.assertEqual(self.tool._insert_at_line("a\nb", 2, "c", before=False)[0], "a\nb\nc")

    def test_insert_after_last_line_with_newline(self):
        self.assertEqual(self.tool._insert_at_line("a\nb\n", 2, "c", before=False)[0], "a\nb\nc\n")


class MarkdownIntrawordEmphasis(unittest.TestCase):
    def test_identifiers_and_arithmetic_are_left_alone(self):
        runs = to_runs("use my_var_name and 2*3*4 here")
        self.assertEqual("".join(text for text, _ in runs), "use my_var_name and 2*3*4 here")

    def test_real_emphasis_still_parses(self):
        texts = [text for text, _ in to_runs("*real* and _also_")]
        self.assertIn("real", texts)
        self.assertIn("also", texts)


class CompactionKeepsToolBatchesIntact(unittest.TestCase):
    def test_kept_window_never_starts_with_orphaned_tool_result(self):
        for pad in range(4):  # shift the split point across every batch position
            with self.subTest(pad=pad):
                self._check(pad)

    def _check(self, pad: int):
        engine = object.__new__(QueryEngine)
        engine._compact_attempts = 1  # keep = 20
        msgs = [Message(role="system", content="sys")]
        # 30 turns of: user, assistant(tool_calls), tool, tool
        for i in range(30):
            msgs.append(Message(role="user", content=f"u{i}"))
            msgs.append(Message(role="assistant", tool_calls=[
                ToolCall(id=f"c{i}", name="bash", arguments={}),
                ToolCall(id=f"d{i}", name="bash", arguments={}),
            ]))
            msgs.append(Message(role="tool", content="r", tool_call_id=f"c{i}", name="bash"))
            msgs.append(Message(role="tool", content="r", tool_call_id=f"d{i}", name="bash"))
        # Tail padding moves the (from-the-end) split point across the batch.
        msgs += [Message(role="user", content=f"pad{i}") for i in range(pad)]
        engine.messages = msgs
        engine._compact()
        # First message after system (+ optional digest/ack pair) must not be a tool result.
        rest = [m for m in engine.messages[1:] if m.role != "user" or not m.content.startswith("[Earlier")]
        first_real = next(m for m in rest if m.role in ("assistant", "tool", "user") and m.content != "Understood. I have the context from the compacted conversation and will continue from where we left off.")
        self.assertNotEqual(first_real.role, "tool")
        # Every tool result still has its parent assistant tool_calls message before it.
        seen_ids: set[str] = set()
        for m in engine.messages:
            if m.role == "assistant":
                seen_ids.update(tc.id for tc in m.tool_calls)
            elif m.role == "tool":
                self.assertIn(m.tool_call_id, seen_ids)


class LocalToLocalProviderSelection(unittest.TestCase):
    def test_reselecting_local_keeps_custom_context_size(self):
        config = TetherConfig()
        config.provider = "local"
        config.context_size = DEFAULT_CONTEXT_SIZE * 2
        config.local_context_size = 0
        apply_provider_selection(config, "local")
        self.assertEqual(config.context_size, DEFAULT_CONTEXT_SIZE * 2)

    def test_local_to_local_with_stash_hands_over_new_model_context(self):
        # The desktop bridge stashes a newly selected model's context length in
        # local_context_size before re-selecting "local"; that must win.
        config = TetherConfig()
        config.provider = "local"
        config.context_size = 32768
        config.local_context_size = 131072
        apply_provider_selection(config, "local")
        self.assertEqual(config.context_size, 131072)
        self.assertEqual(config.local_context_size, 0)


if __name__ == "__main__":
    unittest.main()
