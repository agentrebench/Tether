"""Focused Bash tool safety and error-reporting regressions."""
from __future__ import annotations

from pathlib import Path

from tether.tools.bash import BashTool
from tether.tools.workspace import WorkspacePolicy


def test_nonzero_exit_is_reported_as_command_failure():
    result = BashTool().execute({"command": "printf oops >&2; exit 7"})

    assert result.is_error
    assert result.error_code == "COMMAND_FAILED"
    assert result.metadata["exit_code"] == 7
    assert "exit code: 7" in result.content


def test_enforced_bash_rejects_home_path_before_launch(tmp_path: Path):
    root = tmp_path / "project"
    root.mkdir()
    tool = BashTool(WorkspacePolicy(root, enforced=True))

    result = tool.execute({"command": "cd ~/mlxwork && pwd"})

    assert result.is_error
    assert result.error_code == "WORKSPACE_PATH_DENIED"
    assert "outside the selected workspace" in result.content
