"""Tests for the terminal markdown renderer (inline styling + word wrap)."""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from tether.ui.markdown import to_runs, wrap_styled

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _plain(s):
    return _ANSI.sub("", s)


class ToRuns(unittest.TestCase):
    def test_bold_and_code_become_runs(self):
        runs = to_runs("use `hmac` for **safety**")
        visibles = [v for v, _ in runs]
        self.assertIn("hmac", visibles)
        self.assertIn("safety", visibles)
        # the markdown delimiters are gone from visible text
        self.assertNotIn("`hmac`", "".join(visibles))
        self.assertNotIn("**safety**", "".join(visibles))

    def test_link_keeps_text(self):
        runs = to_runs("see [the docs](http://x)")
        self.assertIn("the docs", [v for v, _ in runs])
        self.assertNotIn("http://x", "".join(v for v, _ in runs))


class WrapStyled(unittest.TestCase):
    def test_wraps_to_width(self):
        text = "word " * 20  # 20 words of len 4
        lines = wrap_styled(text.strip(), width=20)
        for ln in lines:
            self.assertLessEqual(len(_plain(ln)), 20)
        self.assertGreater(len(lines), 1)

    def test_plain_text_roundtrips(self):
        lines = wrap_styled("the quick brown fox", width=80)
        self.assertEqual(_plain(lines[0]), "the quick brown fox")

    def test_styling_does_not_leak_delimiters(self):
        lines = wrap_styled("a **bold** word and `code` too", width=80)
        joined = _plain(" ".join(lines))
        self.assertEqual(joined, "a bold word and code too")

    def test_inline_style_adjacent_to_text_preserved(self):
        # `code`s should render as one word "codes", not "code s"
        lines = wrap_styled("the `code`s here", width=80)
        self.assertIn("codes", _plain(" ".join(lines)))

    def test_never_empty(self):
        self.assertEqual(wrap_styled("", width=40), [""])


if __name__ == "__main__":
    unittest.main()
