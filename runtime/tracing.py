"""Bounded metadata-only traces. Tracing failure must not change agent behavior."""
from __future__ import annotations

from contextlib import contextmanager
import json
import logging
import os
from pathlib import Path
import re
import threading
import time
import uuid

PROTOCOL_VERSION = 1
_LABEL = re.compile(r"[^a-zA-Z0-9_.:/-]")


def label(value):
    return _LABEL.sub("_", str(value))[:128]


class RunTrace:
    MAX_EVENTS = 10000
    MAX_FILES = 100

    def __init__(self, directory, provider, model):
        self.run_id = uuid.uuid4().hex
        self.path = Path(directory).expanduser() / f"{self.run_id}.jsonl"
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if sum(1 for path in self.path.parent.glob("*.jsonl")
               if re.fullmatch(r"[0-9a-f]{32}\.jsonl", path.name)) >= self.MAX_FILES:
            raise ValueError("Trace directory is full; archive traces before recording more")
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        self._file = os.fdopen(fd, "w", encoding="utf-8", buffering=1)
        self._lock = threading.Lock()
        self._started = time.monotonic_ns()
        self._sequence = 0
        self._dropped = 0
        self._closed = False
        self.emit("run.started", provider=label(provider), model=label(model))

    @classmethod
    def open(cls, config):
        directory = getattr(config, "runtime_trace_dir", "")
        if not directory:
            return None
        try:
            return cls(directory, config.provider, config.api_model or "local")
        except (OSError, ValueError):
            logging.getLogger(__name__).warning("Runtime tracing unavailable; turn will continue without a trace")
            return None

    def emit(self, kind, **metrics):
        with self._lock:
            if self._closed:
                return
            if self._sequence >= self.MAX_EVENTS - 1 and kind != "run.finished":
                self._dropped += 1
                return
            self._sequence += 1
            record = dict(version=PROTOCOL_VERSION, run_id=self.run_id, sequence=self._sequence,
                          elapsed_ns=time.monotonic_ns() - self._started, event=kind, **metrics)
            try:
                self._file.write(json.dumps(record, allow_nan=False, separators=(",", ":")) + "\n")
            except (OSError, ValueError, TypeError):
                self._disable()
                logging.getLogger(__name__).warning("Runtime trace write failed; turn will continue")

    def _disable(self):
        self._closed = True
        try:
            self._file.close()
        except OSError:
            pass

    @contextmanager
    def span(self, kind, name):
        started = time.monotonic_ns()
        metrics = {"name": label(name), "status": "returned"}
        try:
            yield metrics
        except BaseException as exc:
            metrics["status"] = "cancelled" if type(exc).__name__ == "CancelledByUser" else "raised"
            raise
        finally:
            self.emit(kind + ".finished", duration_ns=time.monotonic_ns() - started, **metrics)

    def finish(self, status, stop_reason, usage):
        self.emit("run.finished", status=label(status), stop_reason=label(stop_reason),
                  input_tokens=usage[0], output_tokens=usage[1], dropped_events=self._dropped)
        with self._lock:
            self._disable()
