"""Tests for the engine's fragile core: tool-argument coercion, the retry
wrapper, compaction digests, tool-output truncation, error-class signatures,
bash execution/cancellation, and file_edit modes. These paths carry the most
churn and previously had no coverage at all."""
import email.message
import io
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from tether.engine.backend import (
    InferenceBackend,
    MALFORMED_ARGS_KEY,
    RETRY_ATTEMPTS,
    coerce_tool_arguments,
)
from tether.engine.query_engine import QueryEngine
from tether.core.config import TetherConfig
from tether.core.models import Message, ToolCall
from tether.tools.bash import BashTool
from tether.tools.file_edit import FileEditTool


class CoerceToolArguments(unittest.TestCase):
    def test_strict_json(self):
        self.assertEqual(coerce_tool_arguments('{"a": 1}'), {"a": 1})

    def test_dict_passthrough(self):
        d = {"a": 1}
        self.assertIs(coerce_tool_arguments(d), d)

    def test_empty_inputs(self):
        self.assertEqual(coerce_tool_arguments(""), {})
        self.assertEqual(coerce_tool_arguments(None), {})
        self.assertEqual(coerce_tool_arguments("   "), {})

    def test_python_literal_recovery(self):
        self.assertEqual(
            coerce_tool_arguments("{'command': 'ls', 'flag': True, 'x': None}"),
            {"command": "ls", "flag": True, "x": None},
        )

    def test_non_dict_json_is_malformed(self):
        result = coerce_tool_arguments('["not", "a", "dict"]')
        self.assertIn(MALFORMED_ARGS_KEY, result)

    def test_truncated_json_is_malformed(self):
        result = coerce_tool_arguments('{"command": "echo hel')
        self.assertIn(MALFORMED_ARGS_KEY, result)
        self.assertIn("echo hel", result[MALFORMED_ARGS_KEY])

    def test_garbage_is_malformed(self):
        result = coerce_tool_arguments("run the tests please")
        self.assertIn(MALFORMED_ARGS_KEY, result)


def _http_error(code: int, retry_after: str | None = None) -> urllib.error.HTTPError:
    headers = email.message.Message()
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return urllib.error.HTTPError("http://x", code, "boom", headers, io.BytesIO(b""))


class UrlopenRetry(unittest.TestCase):
    def setUp(self):
        self.backend = InferenceBackend(config=mock.Mock())

    def _run(self, side_effects):
        with mock.patch("urllib.request.urlopen", side_effect=side_effects) as opened, \
             mock.patch("tether.engine.backend.time.sleep") as slept:
            result = self.backend._urlopen_retry(mock.Mock(), timeout=1)
        return result, opened, slept

    def test_transient_url_error_then_success(self):
        resp = mock.Mock()
        result, opened, slept = self._run(
            [urllib.error.URLError("refused"), urllib.error.URLError("refused"), resp]
        )
        self.assertIs(result, resp)
        self.assertEqual(opened.call_count, 3)
        self.assertEqual(slept.call_count, 2)

    def test_retryable_http_code_then_success(self):
        resp = mock.Mock()
        result, opened, _ = self._run([_http_error(503), resp])
        self.assertIs(result, resp)
        self.assertEqual(opened.call_count, 2)

    def test_client_error_not_retried(self):
        with mock.patch("urllib.request.urlopen", side_effect=_http_error(400)) as opened, \
             mock.patch("tether.engine.backend.time.sleep"):
            with self.assertRaises(urllib.error.HTTPError):
                self.backend._urlopen_retry(mock.Mock(), timeout=1)
        self.assertEqual(opened.call_count, 1)

    def test_exhaustion_raises_last_error(self):
        effects = [_http_error(429)] * RETRY_ATTEMPTS
        with mock.patch("urllib.request.urlopen", side_effect=effects) as opened, \
             mock.patch("tether.engine.backend.time.sleep"):
            with self.assertRaises(urllib.error.HTTPError):
                self.backend._urlopen_retry(mock.Mock(), timeout=1)
        self.assertEqual(opened.call_count, RETRY_ATTEMPTS)

    def test_retry_after_header_honored(self):
        resp = mock.Mock()
        _, _, slept = self._run([_http_error(429, retry_after="7"), resp])
        slept.assert_called_once_with(7.0)


class _LineStreamingResponse:
    """SSE response that rejects bulk reads to catch first-token buffering."""

    def __init__(self, lines: list[bytes]):
        self._lines = iter(lines)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def __iter__(self):
        return self

    def __next__(self):
        return next(self._lines)

    def read(self, _size=-1):
        raise AssertionError("SSE streams must not use block-buffered read()")


class ChatCompletionStreaming(unittest.TestCase):
    def test_first_sse_line_is_yielded_without_waiting_for_a_block(self):
        backend = InferenceBackend(TetherConfig())
        response = _LineStreamingResponse([
            b'data: {"choices":[{"delta":{"content":"hello"}}]}\n',
            b'data: [DONE]\n',
        ])

        with mock.patch.object(backend, "_urlopen_retry", return_value=response):
            stream = backend.chat_completion_stream([Message(role="user", content="hi")])
            first = next(stream)
            self.assertEqual(first["choices"][0]["delta"]["content"], "hello")
            self.assertEqual(list(stream), [])


class BuildDigest(unittest.TestCase):
    def _dropped(self):
        return [
            Message(role="user", content="fix the bug in parser"),
            Message(
                role="assistant",
                content=None,
                tool_calls=[ToolCall(id="1", name="file_read",
                                     arguments={"file_path": "src/parser.py"})],
            ),
            Message(role="tool", name="file_read", tool_call_id="1",
                    content="1  def parse():\n2      pass\n" * 50),
            Message(
                role="assistant",
                content="Found it, editing now",
                tool_calls=[ToolCall(id="2", name="file_edit",
                                     arguments={"file_path": "src/parser.py"})],
            ),
            Message(role="tool", name="file_edit", tool_call_id="2",
                    content="SUMMARY: edited src/parser.py via exact match"),
        ]

    def test_tool_activity_preserved(self):
        digest = QueryEngine._build_digest(self._dropped(), attempt=1)
        self.assertIn("Tool file_read", digest)
        self.assertIn("SUMMARY: edited src/parser.py", digest)
        self.assertIn("called file_read", digest)

    def test_files_touched_inventory(self):
        digest = QueryEngine._build_digest(self._dropped(), attempt=1)
        self.assertIn("src/parser.py (edited)", digest)

    def test_inventory_survives_budget_exhaustion(self):
        filler = [Message(role="user", content="x" * 500) for _ in range(30)]
        dropped = filler + self._dropped()
        digest = QueryEngine._build_digest(dropped, attempt=2)
        self.assertIn("src/parser.py (edited)", digest)

    def test_empty_dropped(self):
        self.assertEqual(QueryEngine._build_digest([], attempt=1), "")

    def test_edit_verb_not_downgraded_by_later_read(self):
        dropped = [
            Message(role="assistant", content=None, tool_calls=[
                ToolCall(id="1", name="file_edit", arguments={"file_path": "a.py"}),
            ]),
            Message(role="assistant", content=None, tool_calls=[
                ToolCall(id="2", name="file_read", arguments={"file_path": "a.py"}),
            ]),
        ]
        digest = QueryEngine._build_digest(dropped, attempt=1)
        self.assertIn("a.py (edited)", digest)


class TruncateToolContent(unittest.TestCase):
    def test_under_limit_unchanged(self):
        self.assertEqual(QueryEngine._truncate_tool_content("short", 100), "short")

    def test_middle_elision_keeps_head_and_tail(self):
        content = "H" * 200 + "M" * 200 + "T" * 200
        out = QueryEngine._truncate_tool_content(content, max_chars=100)
        self.assertTrue(out.startswith("H" * 50))
        self.assertTrue(out.endswith("T" * 50))
        self.assertIn("truncated", out)


class ErrorClassSignature(unittest.TestCase):
    def test_same_symptom_different_lines_share_signature(self):
        a = QueryEngine._error_class_signature(
            "file_edit", "Error at /Users/x/proj/foo.py line 42: old_string not found")
        b = QueryEngine._error_class_signature(
            "file_edit", "Error at /Users/x/proj/bar.py line 97: old_string not found")
        self.assertEqual(a, b)

    def test_different_tools_differ(self):
        a = QueryEngine._error_class_signature("bash", "command not found")
        b = QueryEngine._error_class_signature("grep", "command not found")
        self.assertNotEqual(a, b)

    def test_empty_error(self):
        self.assertEqual(
            QueryEngine._error_class_signature("bash", "\n\n"), "bash: <empty>")


class BashToolBehavior(unittest.TestCase):
    def setUp(self):
        self.tool = BashTool()

    def test_normal_execution(self):
        result = self.tool.execute({"command": "echo hello"})
        self.assertFalse(result.is_error)
        self.assertIn("hello", result.content)

    def test_stderr_and_exit_code_reported(self):
        result = self.tool.execute({"command": "echo oops >&2; exit 3"})
        self.assertIn("STDERR:", result.content)
        self.assertIn("oops", result.content)
        self.assertIn("exit code: 3", result.content)

    def test_empty_command_is_error(self):
        result = self.tool.execute({"command": ""})
        self.assertTrue(result.is_error)

    def test_timeout_kills_process(self):
        start = time.monotonic()
        result = self.tool.execute({"command": "sleep 30", "timeout": 1})
        self.assertLess(time.monotonic() - start, 10)
        self.assertTrue(result.is_error)
        self.assertIn("timed out", result.content)

    def test_cancel_event_interrupts(self):
        cancel = threading.Event()
        threading.Timer(0.5, cancel.set).start()
        start = time.monotonic()
        result = self.tool.execute_with_cancel(
            {"command": "echo partial; sleep 30"}, cancel)
        self.assertLess(time.monotonic() - start, 10)
        self.assertTrue(result.is_error)
        self.assertIn("cancelled", result.content.lower())
        self.assertIn("partial", result.content)

    def test_cancel_kills_child_processes(self):
        cancel = threading.Event()
        threading.Timer(0.5, cancel.set).start()
        # The sleep is a child of bash; group-kill must reach it. Use a
        # distinctive duration so the check cannot match anything else.
        self.tool.execute_with_cancel(
            {"command": "sleep 300.31 & wait"}, cancel)
        # The signal is delivered synchronously, but the orphaned child is
        # reaped by init on its own schedule (slow on CI); poll briefly and
        # anchor the pattern so the `sh -c pgrep ...` wrapper never matches.
        deadline = time.monotonic() + 5.0
        out = "x"
        while time.monotonic() < deadline:
            out = subprocess.run(
                ["pgrep", "-f", "^sleep 300\\.31$"], capture_output=True, text=True,
            ).stdout.strip()
            if not out:
                break
            time.sleep(0.1)
        self.assertEqual(out, "")

    def test_cwd_persists_between_calls(self):
        self.tool.execute({"command": "cd /tmp"})
        result = self.tool.execute({"command": "pwd"})
        self.assertIn("/tmp", result.content)


class FileEditModes(unittest.TestCase):
    def setUp(self):
        self.tool = FileEditTool()
        fd, self.path = tempfile.mkstemp(suffix=".txt")
        os.close(fd)
        with open(self.path, "w") as f:
            f.write("alpha\nbeta\ngamma\n")

    def tearDown(self):
        os.unlink(self.path)

    def _read(self):
        with open(self.path) as f:
            return f.read()

    def test_exact_replace(self):
        result = self.tool.execute(
            {"file_path": self.path, "old_string": "beta", "new_string": "BETA"})
        self.assertFalse(result.is_error)
        self.assertIn("SUMMARY: edited", result.content)
        self.assertEqual(self._read(), "alpha\nBETA\ngamma\n")

    def test_ambiguous_old_string_rejected(self):
        with open(self.path, "w") as f:
            f.write("dup\ndup\n")
        result = self.tool.execute(
            {"file_path": self.path, "old_string": "dup", "new_string": "x"})
        self.assertTrue(result.is_error)
        self.assertIn("found 2 times", result.content)

    def test_replace_all(self):
        with open(self.path, "w") as f:
            f.write("dup\ndup\n")
        result = self.tool.execute(
            {"file_path": self.path, "old_string": "dup", "new_string": "x",
             "replace_all": True})
        self.assertFalse(result.is_error)
        self.assertEqual(self._read(), "x\nx\n")

    def test_old_string_not_found(self):
        result = self.tool.execute(
            {"file_path": self.path, "old_string": "missing", "new_string": "x"})
        self.assertTrue(result.is_error)

    def test_line_range_replace(self):
        result = self.tool.execute(
            {"file_path": self.path, "start_line": 2, "end_line": 3,
             "new_string": "middle"})
        self.assertFalse(result.is_error)
        self.assertEqual(self._read(), "alpha\nmiddle\n")

    def test_insert_before_and_after(self):
        self.tool.execute(
            {"file_path": self.path, "insert_before_line": 1, "new_string": "top"})
        self.tool.execute(
            {"file_path": self.path, "insert_after_line": 4, "new_string": "bottom"})
        self.assertEqual(self._read(), "top\nalpha\nbeta\ngamma\nbottom\n")

    def test_append(self):
        result = self.tool.execute(
            {"file_path": self.path, "append": True, "new_string": "end\n"})
        self.assertFalse(result.is_error)
        self.assertEqual(self._read(), "alpha\nbeta\ngamma\nend\n")

    def test_no_mode_is_error(self):
        result = self.tool.execute({"file_path": self.path, "new_string": "x"})
        self.assertTrue(result.is_error)
        self.assertIn("No edit mode", result.content)

    def test_two_modes_is_error(self):
        result = self.tool.execute(
            {"file_path": self.path, "old_string": "alpha", "append": True,
             "new_string": "x"})
        self.assertTrue(result.is_error)
        self.assertIn("exactly one edit mode", result.content)

    def test_missing_file_is_error(self):
        result = self.tool.execute(
            {"file_path": "/nonexistent/nope.txt", "old_string": "a",
             "new_string": "b"})
        self.assertTrue(result.is_error)

    def test_batch_edits_apply_in_order(self):
        result = self.tool.execute({
            "file_path": self.path,
            "edits": [
                {"old_string": "alpha", "new_string": "ALPHA"},
                {"old_string": "gamma", "new_string": "GAMMA"},
            ],
        })
        self.assertFalse(result.is_error)
        self.assertIn("applied 2 replacement(s)", result.content)
        self.assertEqual(self._read(), "ALPHA\nbeta\nGAMMA\n")

    def test_batch_edits_atomic_on_failure(self):
        result = self.tool.execute({
            "file_path": self.path,
            "edits": [
                {"old_string": "alpha", "new_string": "ALPHA"},
                {"old_string": "missing", "new_string": "x"},
            ],
        })
        self.assertTrue(result.is_error)
        # first edit must NOT have been written
        self.assertEqual(self._read(), "alpha\nbeta\ngamma\n")

    def test_batch_edits_exclusive_with_other_modes(self):
        result = self.tool.execute({
            "file_path": self.path,
            "old_string": "alpha",
            "new_string": "x",
            "edits": [{"old_string": "beta", "new_string": "y"}],
        })
        self.assertTrue(result.is_error)
        self.assertIn("exactly one edit mode", result.content)

    def test_confidence_marker_emitted(self):
        result = self.tool.execute(
            {"file_path": self.path, "old_string": "beta", "new_string": "B",
             "confidence": 0.85})
        self.assertIn("CONFIDENCE: 0.85", result.content)


if __name__ == "__main__":
    unittest.main()
