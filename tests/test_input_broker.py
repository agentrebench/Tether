"""Tests for the InputBroker request/response handoff (feature #1/#2)."""
import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from tether.session.input_broker import InputBroker


def wait_until(pred, timeout=2.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.005)
    return False


class Broker(unittest.TestCase):
    def test_fulfill_delivers_line(self):
        b = InputBroker()
        box = {}
        t = threading.Thread(target=lambda: box.__setitem__("r", b.request_line("approve?")))
        t.start()
        self.assertTrue(wait_until(b.has_pending))
        self.assertEqual(b.hint(), "approve?")
        self.assertTrue(b.fulfill("y"))
        t.join(2.0)
        self.assertEqual(box["r"], "y")
        self.assertFalse(b.has_pending())

    def test_cancel_returns_none(self):
        b = InputBroker()
        box = {}
        t = threading.Thread(target=lambda: box.__setitem__("r", b.request_line()))
        t.start()
        self.assertTrue(wait_until(b.has_pending))
        self.assertTrue(b.cancel())
        t.join(2.0)
        self.assertIsNone(box["r"])

    def test_fulfill_without_pending_is_false(self):
        b = InputBroker()
        self.assertFalse(b.fulfill("x"))
        self.assertFalse(b.cancel())


if __name__ == "__main__":
    unittest.main()
