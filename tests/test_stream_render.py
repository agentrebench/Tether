"""Tests for StreamingHighlighter: live markdown stripping, code handling, and
the prose gutter (visual hierarchy)."""
import contextlib
import io
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from tether.engine.query_engine import QueryEngine, StreamingHighlighter

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(s):
    return _ANSI.sub("", s)


def _render(text, gutter="| "):
    h = StreamingHighlighter(gutter=gutter)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        h.feed(text)
        h.flush()
    return buf.getvalue()


class StreamRender(unittest.TestCase):
    def test_prose_gets_gutter(self):
        out = _render("The plan is to add a helper.\n")
        self.assertIn("| The plan is to add a helper.", out)

    def test_markdown_stripped_from_prose(self):
        out = _render("This is **important** and `inline` stuff.\n")
        self.assertNotIn("**", out)
        self.assertNotIn("`", out)
        self.assertIn("important", out)

    def test_fenced_code_not_shown_raw(self):
        out = _render("```python\nx = 1\n```\n")
        # Code is rendered (possibly syntax-highlighted) but the raw fence is gone.
        self.assertIn("x = 1", _strip_ansi(out))
        self.assertNotIn("```", out)

    def test_partial_line_flushed(self):
        # No trailing newline — flush() must still emit it.
        out = _render("tail with no newline")
        self.assertIn("tail with no newline", out)

    def test_each_prose_line_guttered(self):
        out = _render("first line here\nsecond line here\n")
        self.assertGreaterEqual(out.count("| "), 2)


class ReadBufferRender(unittest.TestCase):
    def test_mixed_absolute_and_relative_paths_do_not_raise(self):
        engine = object.__new__(QueryEngine)
        engine._read_buffer = [
            ("file_read", "src/widget.py", "12 lines", False),
            ("file_read", "/tmp/external/config.py", "8 lines", False),
        ]
        buf = io.StringIO()

        with contextlib.redirect_stdout(buf):
            engine._flush_read_buffer()

        rendered = _strip_ansi(buf.getvalue())
        self.assertIn("widget.py", rendered)
        self.assertIn("config.py", rendered)
        self.assertEqual(engine._read_buffer, [])


if __name__ == "__main__":
    unittest.main()
