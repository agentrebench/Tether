"""Tests for color-enable detection (NO_COLOR / non-TTY / FORCE_COLOR)."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from tether.ui import colors


class _FakeStdout:
    def __init__(self, tty):
        self._tty = tty

    def isatty(self):
        return self._tty


class ColorEnabled(unittest.TestCase):
    def _enabled(self, env, tty):
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch.object(colors.sys, "stdout", _FakeStdout(tty)):
            return colors._color_enabled()

    def test_no_color_disables(self):
        self.assertFalse(self._enabled({"NO_COLOR": "1"}, tty=True))
        self.assertFalse(self._enabled({"NO_COLOR": ""}, tty=True))  # presence, not value

    def test_force_color_overrides_everything(self):
        self.assertTrue(self._enabled({"FORCE_COLOR": "1", "NO_COLOR": "1"}, tty=False))

    def test_tty_enables_nonpipe_disables(self):
        self.assertTrue(self._enabled({}, tty=True))
        self.assertFalse(self._enabled({}, tty=False))


if __name__ == "__main__":
    unittest.main()
