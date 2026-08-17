"""Tests for TurnController: the background worker + /also queue + redirect/interrupt."""
import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from tether.engine.turn_controller import TurnController


def wait_until(pred, timeout=2.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.005)
    return False


class FakeEngine:
    """submit() blocks until `release` is set, but returns immediately if its
    cancel_event fires — modelling a real turn that can be interrupted."""

    def __init__(self):
        self.calls = []
        self.release = threading.Event()

    def submit(self, text, cancel_event=None):
        self.calls.append(text)
        while not self.release.is_set():
            if cancel_event is not None and cancel_event.is_set():
                return
            time.sleep(0.005)


class FeedAndQueue(unittest.TestCase):
    def test_feed_idle_starts_turn(self):
        eng = FakeEngine()
        eng.release.set()  # let turns complete immediately
        tc = TurnController(eng)
        self.assertEqual(tc.feed("A"), "started")
        self.assertTrue(wait_until(lambda: not tc.is_busy()))
        self.assertEqual(eng.calls, ["A"])

    def test_feed_busy_queues_and_runs_after(self):
        eng = FakeEngine()  # turns block until released
        tc = TurnController(eng)
        self.assertEqual(tc.feed("A"), "started")
        self.assertTrue(wait_until(lambda: eng.calls == ["A"]))
        self.assertTrue(tc.is_busy())
        self.assertEqual(tc.feed("B"), "queued")
        self.assertEqual(tc.feed("C"), "queued")
        self.assertEqual(tc.queued(), ["B", "C"])
        eng.release.set()  # let A finish; B+C should combine into one next turn
        self.assertTrue(wait_until(lambda: not tc.is_busy()))
        self.assertEqual(eng.calls, ["A", "B\nC"])

    def test_redirect_cancels_current_and_runs_next(self):
        eng = FakeEngine()
        tc = TurnController(eng)
        tc.feed("A")
        self.assertTrue(wait_until(lambda: eng.calls == ["A"]))
        tc.feed("queued-also")  # should be discarded by redirect
        tc.redirect("C")
        # A is cancelled, C becomes the next turn (still blocking)
        self.assertTrue(wait_until(lambda: eng.calls == ["A", "C"]))
        self.assertEqual(tc.queued(), [])  # /also was cleared
        eng.release.set()
        self.assertTrue(wait_until(lambda: not tc.is_busy()))
        self.assertEqual(eng.calls, ["A", "C"])

    def test_interrupt_cancels_and_drops_queue(self):
        eng = FakeEngine()
        tc = TurnController(eng)
        tc.feed("A")
        self.assertTrue(wait_until(lambda: eng.calls == ["A"]))
        tc.feed("B")  # queued
        self.assertTrue(tc.interrupt())
        self.assertTrue(wait_until(lambda: not tc.is_busy()))
        self.assertEqual(eng.calls, ["A"])  # B dropped, nothing new ran
        self.assertEqual(tc.queued(), [])

    def test_redirect_when_idle_just_starts(self):
        eng = FakeEngine()
        eng.release.set()
        tc = TurnController(eng)
        tc.redirect("solo")
        self.assertTrue(wait_until(lambda: not tc.is_busy()))
        self.assertEqual(eng.calls, ["solo"])

    def test_on_idle_called(self):
        eng = FakeEngine()
        eng.release.set()
        idle = threading.Event()
        tc = TurnController(eng, on_idle=idle.set)
        tc.feed("A")
        self.assertTrue(idle.wait(2.0))

    def test_error_in_submit_does_not_kill_worker(self):
        class Boom(FakeEngine):
            def submit(self, text, cancel_event=None):
                self.calls.append(text)
                if text == "A":
                    raise RuntimeError("bad turn")
                # B completes fine

        eng = Boom()
        eng.release.set()
        errors = []
        tc = TurnController(eng, on_error=errors.append)
        tc.feed("A")
        self.assertTrue(wait_until(lambda: not tc.is_busy()))
        self.assertEqual(len(errors), 1)
        # worker survived; a subsequent feed still runs
        tc.feed("B")
        self.assertTrue(wait_until(lambda: eng.calls == ["A", "B"]))


if __name__ == "__main__":
    unittest.main()
