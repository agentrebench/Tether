"""Spinner behavior. With the prompt-on-demand REPL the spinner streams to
stderr while the engine owns the terminal (no patch_stdout), so it animates
normally. The _started guard must keep stop() a no-op when start() never ran."""
import io
import os
import sys
import time
import unittest
import contextlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from tether.engine.query_engine import Spinner


class SpinnerBehavior(unittest.TestCase):
    def test_animates_on_stderr(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            sp = Spinner("Thinking")
            sp.start()
            time.sleep(0.25)
            sp.stop()
        self.assertTrue(sp._started)
        self.assertNotEqual(err.getvalue(), "")  # frames were emitted

    def test_stop_without_start_is_noop(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            sp = Spinner("Thinking")
            sp.stop()  # never started
        self.assertEqual(err.getvalue(), "")  # no stray clear sequence
        self.assertFalse(sp._started)


if __name__ == "__main__":
    unittest.main()
