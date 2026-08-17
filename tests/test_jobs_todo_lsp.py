from __future__ import annotations

import os
import time
from pathlib import Path

from tether.tools.jobs import JobKillTool, JobOutputTool, JobRegistry
from tether.tools.lsp import LspTool
from tether.tools.todo import TodoStore, TodoWriteTool
from tether.tools.workspace import WorkspacePolicy


def _wait_for_job(registry: JobRegistry, job_id: str, timeout: float = 3.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snapshot = registry.snapshot(job_id)
        assert snapshot is not None
        if snapshot["status"] != "running":
            return snapshot
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} did not finish")


def test_background_job_captures_output_and_status(tmp_path: Path):
    registry = JobRegistry()
    started = registry.start_process(
        kind="test",
        label="say hello",
        argv=["bash", "-c", "printf hello"],
        cwd=str(tmp_path),
    )
    snapshot = _wait_for_job(registry, started["id"])
    result = JobOutputTool(registry).execute({"job_id": started["id"]})

    assert snapshot["status"] == "completed"
    assert "hello" in result.content
    assert result.metadata["job"]["exit_code"] == 0


def test_background_job_can_be_stopped(tmp_path: Path):
    registry = JobRegistry()
    started = registry.start_process(
        kind="test",
        label="wait",
        argv=["bash", "-c", "sleep 30"],
        cwd=str(tmp_path),
    )
    result = JobKillTool(registry).execute({"job_id": started["id"]})
    snapshot = _wait_for_job(registry, started["id"])

    assert not result.is_error
    assert snapshot["status"] == "cancelled"


def test_todos_persist_privately_and_emit_updates(tmp_path: Path):
    updates: list[list[dict]] = []
    store = TodoStore(tmp_path / "state" / "todos.json")
    tool = TodoWriteTool(store=store, on_update=updates.append)
    todos = [
        {"content": "Inspect", "status": "completed"},
        {"content": "Build", "status": "in_progress", "active_form": "Building"},
    ]

    result = tool.execute({"todos": todos})
    restored = TodoWriteTool(store=store)

    assert not result.is_error
    assert restored.snapshot() == todos
    assert updates == [todos]
    assert os.stat(store.path).st_mode & 0o777 == 0o600


def test_todos_reject_multiple_in_progress_items():
    result = TodoWriteTool().execute({
        "todos": [
            {"content": "One", "status": "in_progress"},
            {"content": "Two", "status": "in_progress"},
        ]
    })

    assert result.is_error
    assert result.error_code == "INVALID_TODOS"


class _FakeLspProvider:
    def request(self, **kwargs):
        return {
            "uri": kwargs["file_path"].as_uri(),
            "operation": kwargs["operation"],
            "line": kwargs["line"],
        }


def test_lsp_tool_enforces_workspace_and_normalizes_result(tmp_path: Path):
    root = tmp_path / "project"
    root.mkdir()
    source = root / "main.py"
    source.write_text("answer = 42\n")
    policy = WorkspacePolicy(root, enforced=True)
    tool = LspTool(policy, provider=_FakeLspProvider())

    result = tool.execute({
        "operation": "definition", "file_path": "main.py", "line": 1, "character": 1
    })
    denied = tool.execute({
        "operation": "definition", "file_path": "../other.py", "line": 1, "character": 1
    })

    assert not result.is_error
    assert '"operation": "definition"' in result.content
    assert result.display_kind == "code_navigation"
    assert denied.error_code == "WORKSPACE_PATH_DENIED"

