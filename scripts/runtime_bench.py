#!/usr/bin/env python3
"""Offline end-to-end Python baseline with real edits and independent checks.

The model is a deterministic replay fixture. These results measure harness
overhead and correctness, not model quality or real-provider completion time.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import math
import os
from pathlib import Path
import platform
import statistics
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tether.core.config import TetherConfig
from tether.core.models import Message, ToolCall
from tether.core.permissions import PermissionContext
from tether.engine.query_engine import QueryEngine
from tether.tools.base import ToolRegistry
from tether.tools.file_edit import FileEditTool
from tether.tools.file_read import FileReadTool
from tether.tools.workspace import WorkspacePolicy

CASES = {
    "small-edit": {
        "files": {"greeting.py": "def greet(name):\n    return f'Hello, {name}!'\n"},
        "edits": [("greeting.py", "Hello,", "Welcome,")],
        "objective": "Change the greeting in greeting.py to Welcome, keeping name and punctuation.",
        "check": "from greeting import greet\nassert greet('Ada') == 'Welcome, Ada!'\n",
    },
    "bug-fix": {
        "files": {"numbers.py": "def clamp(value, low, high):\n    return min(value, low)\n"},
        "edits": [("numbers.py", "min(value, low)", "min(max(value, low), high)")],
        "objective": "Fix clamp in numbers.py so values below, within, and above the interval are handled correctly.",
        "check": "from numbers import clamp\nassert [clamp(x, 1, 4) for x in (-2, 1, 3, 4, 8)] == [1, 1, 3, 4, 4]\n",
    },
    "multi-file": {
        "files": {"operations.py": "def multiply(a, b):\n    return a + b\n",
                  "service.py": "from operations import multiply\ndef square(value):\n    return multiply(value, 1)\n"},
        "edits": [("operations.py", "a + b", "a * b"), ("service.py", "multiply(value, 1)", "multiply(value, value)")],
        "objective": "Fix multiply and square across operations.py and service.py, including zero and negative inputs.",
        "check": "from operations import multiply\nfrom service import square\nassert multiply(3, 4) == 12\nassert [square(x) for x in (-3, 0, 4)] == [9, 0, 16]\n",
    },
}


class FixtureBackend:
    def __init__(self, case):
        self.responses = [Message("assistant", tool_calls=[
            ToolCall(f"read-{i}", "file_read", {"file_path": name}) for i, name in enumerate(case["files"])
        ])]
        # Separate turns preserve ordering for dependent multi-file changes.
        for i, (name, before, after) in enumerate(case["edits"]):
            self.responses.append(Message("assistant", tool_calls=[ToolCall(f"edit-{i}", "file_edit", {
                "file_path": name, "old_string": before, "new_string": after,
            })]))
        self.responses.append(Message("assistant", "Changes applied. Independent acceptance checks are next."))
        self.calls = 0

    def chat_completion_stream_parsed(self, **kwargs):
        if self.calls >= len(self.responses):
            raise RuntimeError("Unexpected model retry in deterministic baseline")
        response = self.responses[self.calls]
        self.calls += 1
        return response, {"prompt_tokens": 100, "completion_tokens": 20}


def git(root, *args):
    env = dict(os.environ, GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_AUTHOR_DATE="2000-01-01T00:00:00+00:00", GIT_COMMITTER_DATE="2000-01-01T00:00:00+00:00")
    return subprocess.check_output(["git", "-c", "core.hooksPath=" + os.devnull,
                                    "-c", "user.name=Tether Fixture", "-c", "user.email=fixture@localhost",
                                    "-C", str(root), *args], env=env, timeout=10).decode().strip()


def run_case(output, name, case, repetition, tracing):
    directory = output / f"{name}-{repetition}-{'trace' if tracing else 'plain'}"
    workspace = directory / "workspace"
    workspace.mkdir(parents=True)
    for filename, content in case["files"].items():
        (workspace / filename).write_text(content)
    git(workspace, "init", "-q", "--initial-branch=main")
    git(workspace, "add", ".")
    git(workspace, "commit", "-qm", "Pinned engineering fixture v1")
    base = git(workspace, "rev-parse", "HEAD")
    acceptance = directory / "acceptance.py"
    acceptance.write_text("import sys\nsys.path.insert(0, sys.argv[1])\n" + case["check"])
    command = [sys.executable, "-I", "-B", str(acceptance), str(workspace)]
    before = subprocess.run(command, capture_output=True, text=True, timeout=10)
    if before.returncode == 0:
        raise RuntimeError("Acceptance fixture must fail before editing")
    config = TetherConfig(max_turns=8, codebase_model_enabled=False, codebase_model_auto_learn=False,
                          runtime_trace_dir=str(directory / "traces") if tracing else "")
    policy = WorkspacePolicy(workspace, enforced=True)
    registry = ToolRegistry({"file_read": FileReadTool(policy), "file_edit": FileEditTool(workspace=policy)})
    previous = Path.cwd()
    started = time.perf_counter()
    try:
        os.chdir(workspace)
        engine = QueryEngine(config, registry, PermissionContext.from_config(config),
                             persistent_context_enabled=False, include_last_session_summary=False)
        engine.backend = FixtureBackend(case)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            result = engine.submit(case["objective"])
    finally:
        os.chdir(previous)
        registry.close()
    turn_seconds = time.perf_counter() - started
    verified = subprocess.run(command, capture_output=True, text=True, timeout=10)
    total_seconds = time.perf_counter() - started
    log = directory / "verification.log"
    log.write_text(verified.stdout + verified.stderr)
    patch = git(workspace, "diff", "--no-ext-diff", "--binary", "HEAD") + "\n"
    patch_file = directory / "change.patch"
    patch_file.write_text(patch)
    # Reverse-apply check proves the recorded patch matches the final tree.
    applied = subprocess.run(["git", "-C", str(workspace), "apply", "--reverse", "--check", str(patch_file)],
                             capture_output=True, text=True, timeout=10)
    status = "COMPLETED" if verified.returncode == 0 and applied.returncode == 0 and patch.strip() else "FAILED"
    return dict(case=name, repetition=repetition, tracing=tracing, status=status, base_revision=base,
                turn_seconds=turn_seconds, verified_seconds=total_seconds, model_calls=engine.backend.calls,
                simulated_input_tokens=result.usage.input_tokens, simulated_output_tokens=result.usage.output_tokens,
                tool_calls=len(result.tool_calls_made), stop_reason=result.stop_reason,
                patch_sha256=hashlib.sha256(patch.encode()).hexdigest(), patch_path=str(patch_file),
                verification=dict(argv=command, exit_code=verified.returncode, output=str(log),
                                  initial_exit_code=before.returncode, patch_check_exit_code=applied.returncode))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path, help="New directory for evidence and report")
    parser.add_argument("--repetitions", type=int, default=5)
    args = parser.parse_args(argv)
    if not 1 <= args.repetitions <= 30:
        parser.error("repetitions must be in 1..30")
    output = args.output.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=False)
    rows = []
    for repetition in range(args.repetitions):
        # Alternate the order to reduce warm-cache bias.
        for tracing in ((False, True) if repetition % 2 == 0 else (True, False)):
            for name, case in CASES.items():
                rows.append(run_case(output, name, case, repetition, tracing))
    summaries = []
    for name in CASES:
        for tracing in (False, True):
            selected = [row for row in rows if row["case"] == name and row["tracing"] == tracing]
            durations = sorted(row["verified_seconds"] for row in selected)
            summaries.append(dict(case=name, tracing=tracing, samples=len(durations),
                                  passed=sum(row["status"] == "COMPLETED" for row in selected),
                                  p50_seconds=statistics.median(durations),
                                  p95_seconds=durations[math.ceil(len(durations) * .95) - 1]))
    root = Path(__file__).resolve().parents[1]
    digest = hashlib.sha256()
    for folder in ("core", "engine", "tools", "runtime"):
        for file in sorted((root / folder).rglob("*.py")):
            digest.update(str(file.relative_to(root)).encode() + b"\0" + file.read_bytes())
    report = dict(version=1, backend="deterministic-replay-v1", real_provider_measurement=False,
                  python=platform.python_version(), platform=platform.platform(), source_sha256=digest.hexdigest(),
                  summaries=summaries, runs=rows)
    (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(dict(report=str(output / "report.json"), summaries=summaries), indent=2))
    return 0 if all(row["status"] == "COMPLETED" for row in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
