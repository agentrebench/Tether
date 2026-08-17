"""Process-wide background job registry and model-facing controls."""
from __future__ import annotations

import codecs
import os
import signal
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from ..core.models import ToolResult
from .base import BaseTool


@dataclass
class JobRecord:
    id: str
    kind: str
    label: str
    cwd: str
    process: subprocess.Popen[str]
    status: str = "running"
    output: str = ""
    output_omitted: int = 0
    exit_code: int | None = None
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    cancel_requested: bool = False
    timed_out: bool = False


class JobRegistry:
    """Own background processes and bounded output for one agent session."""

    def __init__(self, *, max_output_chars: int = 200_000):
        self.max_output_chars = max(10_000, max_output_chars)
        self._jobs: dict[str, JobRecord] = {}
        self._lock = threading.RLock()

    @staticmethod
    def _terminate_process(proc: subprocess.Popen[str]) -> None:
        if proc.poll() is not None:
            return
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                proc.kill()

    def start_process(
        self,
        *,
        kind: str,
        label: str,
        argv: list[str],
        cwd: str,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        proc = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            cwd=cwd,
            start_new_session=True,
            bufsize=1,
        )
        job_id = f"job_{uuid.uuid4().hex[:10]}"
        record = JobRecord(
            id=job_id,
            kind=kind,
            label=label,
            cwd=cwd,
            process=proc,
        )
        with self._lock:
            self._jobs[job_id] = record

        reader = threading.Thread(
            target=self._collect_output,
            args=(record,),
            name=f"tether-{job_id}",
            daemon=True,
        )
        reader.start()
        if timeout and timeout > 0:
            timer = threading.Timer(timeout, self._timeout, args=(job_id,))
            timer.daemon = True
            timer.start()
        return self.snapshot(job_id) or {}

    def _append(self, record: JobRecord, chunk: str) -> None:
        with self._lock:
            record.output += chunk
            overflow = len(record.output) - self.max_output_chars
            if overflow > 0:
                record.output = record.output[overflow:]
                record.output_omitted += overflow

    def _collect_output(self, record: JobRecord) -> None:
        assert record.process.stdout is not None
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        try:
            while True:
                raw = os.read(record.process.stdout.fileno(), 4096)
                if not raw:
                    tail = decoder.decode(b"", final=True)
                    if tail:
                        self._append(record, tail)
                    break
                chunk = decoder.decode(raw)
                if chunk:
                    self._append(record, chunk)
        finally:
            code = record.process.wait()
            with self._lock:
                record.exit_code = code
                record.finished_at = time.time()
                if record.timed_out:
                    record.status = "timed_out"
                elif record.cancel_requested:
                    record.status = "cancelled"
                else:
                    record.status = "completed" if code == 0 else "failed"

    def _timeout(self, job_id: str) -> None:
        with self._lock:
            record = self._jobs.get(job_id)
            if record is None or record.process.poll() is not None:
                return
            record.timed_out = True
            proc = record.process
        self._terminate_process(proc)

    def snapshot(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            record = self._jobs.get(job_id)
            if record is None:
                return None
            return {
                "id": record.id,
                "kind": record.kind,
                "label": record.label,
                "cwd": record.cwd,
                "status": record.status,
                "exit_code": record.exit_code,
                "started_at": record.started_at,
                "finished_at": record.finished_at,
                "output_chars": len(record.output),
                "output_omitted": record.output_omitted,
            }

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            ids = list(self._jobs)
        return [snapshot for job_id in ids if (snapshot := self.snapshot(job_id))]

    def output(self, job_id: str, *, tail_chars: int = 30_000) -> tuple[dict[str, Any], str] | None:
        snapshot = self.snapshot(job_id)
        if snapshot is None:
            return None
        with self._lock:
            record = self._jobs[job_id]
            output = record.output[-max(1_000, min(int(tail_chars), 100_000)):]
            omitted = record.output_omitted + max(0, len(record.output) - len(output))
        if omitted:
            output = f"[... {omitted} earlier characters omitted ...]\n{output}"
        return snapshot, output

    def kill(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            record = self._jobs.get(job_id)
            if record is None:
                return None
            if record.process.poll() is not None:
                return self.snapshot(job_id)
            record.cancel_requested = True
            proc = record.process
        self._terminate_process(proc)
        with self._lock:
            if record.status == "running":
                record.status = "cancelled"
        return self.snapshot(job_id)

    def close(self) -> None:
        for snapshot in self.list():
            if snapshot["status"] == "running":
                self.kill(snapshot["id"])


class JobListTool(BaseTool):
    def __init__(self, registry: JobRegistry):
        self.registry = registry

    @property
    def name(self) -> str:
        return "job_list"

    @property
    def description(self) -> str:
        return "List shell and other background jobs started in this Tether session."

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {}}

    def execute(self, arguments: dict) -> ToolResult:
        jobs = self.registry.list()
        if not jobs:
            return ToolResult(name=self.name, content="No background jobs.", display_kind="jobs")
        lines = [
            f"{job['id']}  {job['status']}  {job['label']}"
            + (f"  (exit {job['exit_code']})" if job["exit_code"] is not None else "")
            for job in jobs
        ]
        return ToolResult(
            name=self.name, content="\n".join(lines), display_kind="jobs",
            metadata={"jobs": jobs},
        )


class JobOutputTool(BaseTool):
    def __init__(self, registry: JobRegistry):
        self.registry = registry

    @property
    def name(self) -> str:
        return "job_output"

    @property
    def description(self) -> str:
        return "Read the latest bounded output and status for one background job."

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "job_id": {"type": "string"},
                "tail_chars": {"type": "integer", "default": 30000},
            },
            "required": ["job_id"],
        }

    def execute(self, arguments: dict) -> ToolResult:
        job_id = str(arguments.get("job_id", ""))
        found = self.registry.output(job_id, tail_chars=int(arguments.get("tail_chars") or 30000))
        if found is None:
            return ToolResult(
                name=self.name, content=f"Unknown background job: {job_id}",
                is_error=True, error_code="JOB_NOT_FOUND",
            )
        snapshot, output = found
        content = output.rstrip() or "(no output yet)"
        content += f"\n\n[status: {snapshot['status']}]"
        return ToolResult(
            name=self.name, content=content, display_kind="terminal",
            metadata={"job": snapshot},
        )


class JobKillTool(BaseTool):
    def __init__(self, registry: JobRegistry):
        self.registry = registry

    @property
    def name(self) -> str:
        return "job_kill"

    @property
    def description(self) -> str:
        return "Stop one background job that Tether started in this session."

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {"job_id": {"type": "string"}},
            "required": ["job_id"],
        }

    def execute(self, arguments: dict) -> ToolResult:
        job_id = str(arguments.get("job_id", ""))
        snapshot = self.registry.kill(job_id)
        if snapshot is None:
            return ToolResult(
                name=self.name, content=f"Unknown background job: {job_id}",
                is_error=True, error_code="JOB_NOT_FOUND",
            )
        return ToolResult(
            name=self.name,
            content=f"Background job {job_id} is {snapshot['status']}.",
            display_kind="jobs", metadata={"job": snapshot},
        )
