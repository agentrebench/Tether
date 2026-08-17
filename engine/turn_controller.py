"""Background turn orchestration for live type-during-generation (features #1/#2).

The engine keeps single-threaded state (one `messages` list), so exactly one
thread may ever call `engine.submit`. This controller is that thread: it runs
turns in the background while the main thread keeps the keyboard, and exposes a
small thread-safe surface for what the user can do mid-generation:

  - feed(text)      idle  -> start a turn;  busy -> queue as an /also follow-up
                    (processed as the next turn when the current one finishes)
  - redirect(text)  cancel the current turn now and run `text` next instead
  - interrupt()     cancel the current turn and drop anything queued

All disposition decisions are made under a single lock so there is no race
between "is a turn running?" and acting on the answer.
"""
from __future__ import annotations

import threading
import time


# Thread-local marker: True only on the worker thread while it is running the
# user's primary turn. Sub-agents run in fresh executor threads that do NOT
# inherit this, so they correctly read as non-interactive (a sub-agent must not
# try to prompt the user). Tools consult is_interactive_turn() to decide whether
# reaching the user is possible.
_tl = threading.local()


def mark_interactive(value: bool) -> None:
    _tl.interactive = value


def is_interactive_turn() -> bool:
    return getattr(_tl, "interactive", False)


class TurnController:
    def __init__(self, engine, on_idle=None, on_error=None, on_turn_complete=None):
        self.engine = engine
        self._on_idle = on_idle      # called (no args) when the worker goes idle
        self._on_error = on_error    # called (exc) if a submit raises
        self._on_turn_complete = on_turn_complete  # called (TurnResult) per turn

        self._lock = threading.Lock()
        self._running = False
        self._thread: threading.Thread | None = None
        self._also: list[str] = []          # queued follow-ups
        self._redirect: str | None = None   # takes priority over _also
        self._cancel_event: threading.Event | None = None  # live turn's cancel

    # -- introspection -----------------------------------------------------
    def is_busy(self) -> bool:
        with self._lock:
            return self._running

    def queued(self) -> list[str]:
        with self._lock:
            return list(self._also)

    # -- user actions ------------------------------------------------------
    def feed(self, text: str) -> str:
        """Idle -> start a turn ('started'). Busy -> queue as /also ('queued')."""
        with self._lock:
            if self._running:
                self._also.append(text)
                return "queued"
            self._running = True
            self._thread = threading.Thread(
                target=self._worker, args=(text,), daemon=True
            )
            self._thread.start()
            return "started"

    def redirect(self, text: str) -> str:
        """Cancel the current turn and run `text` next. If idle, just starts it.
        Pending /also items are discarded — redirecting means changing course."""
        with self._lock:
            if not self._running:
                # fall through to a normal start below (outside the lock guard)
                start_now = True
            else:
                start_now = False
                self._redirect = text
                self._also.clear()
                ev = self._cancel_event
        if start_now:
            return self.feed(text)
        if ev is not None:
            ev.set()
        return "redirected"

    def interrupt(self) -> bool:
        """Cancel the current turn and drop anything queued. Returns whether a
        turn was actually running."""
        with self._lock:
            self._also.clear()
            self._redirect = None
            ev = self._cancel_event
            running = self._running
        if ev is not None:
            ev.set()
        return running

    def wait(self, timeout: float | None = None) -> None:
        """Block until the worker is idle (for shutdown / tests)."""
        t = self._thread
        if t is not None:
            t.join(timeout)

    # -- worker ------------------------------------------------------------
    def _worker(self, current: str) -> None:
        # This thread is servicing the user's primary turn: tools may reach the
        # user (via the InputBroker). Sub-agent threads won't see this flag.
        mark_interactive(True)
        while current is not None:
            ev = threading.Event()
            with self._lock:
                self._cancel_event = ev
            try:
                t0 = time.monotonic()
                result = self.engine.submit(current, cancel_event=ev)
                if result is not None and self._on_turn_complete is not None:
                    self._on_turn_complete(result, time.monotonic() - t0)
            except Exception as exc:  # keep the loop alive across a bad turn
                if self._on_error is not None:
                    try:
                        self._on_error(exc)
                    except Exception:
                        # A raising callback must not kill the worker with
                        # _running=True (feed() would then queue forever).
                        pass
            current = self._next_input()
        if self._on_idle is not None:
            self._on_idle()

    def _next_input(self) -> str | None:
        """Pick the next turn's input, or mark idle. Runs under the lock so a
        concurrent feed()/redirect() is ordered consistently with going idle."""
        with self._lock:
            if self._redirect is not None:
                nxt, self._redirect = self._redirect, None
                return nxt
            if self._also:
                nxt = "\n".join(self._also)
                self._also.clear()
                return nxt
            self._running = False
            self._cancel_event = None
            return None
