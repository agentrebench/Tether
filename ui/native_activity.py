"""Opt-in, content-free activity signals for the native terminal host."""
from __future__ import annotations

from contextlib import contextmanager
import os
import sys
import threading


class NativeActivity:
    """Emit bounded OSC messages only when the native host explicitly opts in.

    This is presentation metadata, never a permission or execution channel.
    Each emitter belongs to one agent PTY. Private model text is never inspected.
    """

    def __init__(self, *, enabled=None, write=None):
        if enabled is None:
            enabled = os.environ.get("TETHER_NATIVE_ACTIVITY") == "1" and sys.stdout.isatty()
        self.enabled = bool(enabled)
        self._write = write or (lambda data: os.write(1, data))
        self._lock = threading.RLock()
        self._sequence = 0
        self._last = None
        self._phase = "idle"
        self._active = False
        self._waiting = 0
        self._agents = {}

    def _emit(self):
        if not self.enabled:
            return
        phase = self._phase
        if self._waiting:
            phase = "waiting"
        elif self._active and phase not in {"thinking", "reasoning"}:
            if any(item[1] == "thinking" for item in self._agents.values()):
                phase = "thinking"
        if phase == self._last:
            return
        self._sequence += 1
        self._last = phase
        try:
            self._write(f"\x1b]777;tether;1;{self._sequence};{phase}\x07".encode("ascii"))
        except (OSError, ValueError):
            self.enabled = False

    def stream(self, event):
        if not self.enabled:
            return
        with self._lock:
            if event.type == "model_started":
                self._active = True
                self._phase = "thinking"
            elif self._active and event.type == "thinking":
                self._phase = "reasoning"
            elif self._active and event.type in {
                "model_finished", "text", "tool_running", "tool_done", "tool_code", "checkpoint", "complete",
            }:
                self._phase = "working"
            self._emit()

    def agent(self, snapshot):
        if not self.enabled:
            return
        with self._lock:
            if not self._active:
                return
            agent_id = snapshot.get("agent_id")
            revision = snapshot.get("revision")
            if not isinstance(agent_id, str) or not isinstance(revision, int):
                return
            previous = self._agents.get(agent_id)
            if previous and revision <= previous[0]:
                return
            if not previous and len(self._agents) >= 64:
                return
            phase = "thinking" if snapshot.get("phase") == "thinking" and snapshot.get("status") == "running" else "working"
            self._agents[agent_id] = (revision, phase)
            self._emit()

    @contextmanager
    def waiting(self):
        with self._lock:
            self._waiting += 1
            self._emit()
        try:
            yield
        finally:
            with self._lock:
                self._waiting = max(0, self._waiting - 1)
                self._emit()

    def idle(self):
        with self._lock:
            self._active = False
            self._phase = "idle"
            self._agents.clear()
            self._emit()
