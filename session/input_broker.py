"""Single-slot input channel between a background turn and the main loop.

While a turn runs on the TurnController worker thread, the main thread is the
only reader of the keyboard. When the worker needs a line of input from the user
mid-turn — a tool approval, or an answer to a reverse clarifying question — it
can't read stdin itself. It posts the request here and blocks; the main loop
notices the pending request, routes the next typed line to it, and the worker
resumes.

One slot, serviced strictly one request at a time. The worker always blocks
between requests, so there is never more than one outstanding.
"""
from __future__ import annotations

import threading


class InputBroker:
    _CANCEL = object()

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending = False
        self._hint = ""
        self._response = None
        self._event = threading.Event()

    # -- worker side -------------------------------------------------------
    def request_line(self, hint: str = "") -> str | None:
        """Block until the main loop fulfills (returns the line) or cancels
        (returns None). Called on the worker thread."""
        with self._lock:
            self._pending = True
            self._hint = hint
            self._response = None
            self._event.clear()
        self._event.wait()
        with self._lock:
            self._pending = False
            resp = self._response
            self._response = None
        return None if resp is self._CANCEL else resp

    # -- main-loop side ----------------------------------------------------
    def has_pending(self) -> bool:
        with self._lock:
            return self._pending

    def hint(self) -> str:
        with self._lock:
            return self._hint

    def fulfill(self, line: str) -> bool:
        """Deliver a typed line to the waiting worker. Returns False if nothing
        was waiting."""
        with self._lock:
            if not self._pending:
                return False
            self._response = line
            self._event.set()
            return True

    def cancel(self) -> bool:
        """Unblock a waiting worker with a cancellation (request_line -> None)."""
        with self._lock:
            if not self._pending:
                return False
            self._response = self._CANCEL
            self._event.set()
            return True
