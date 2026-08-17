from __future__ import annotations

import platform
import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from tether.tools.file_read import FileReadTool
from tether.tools.file_write import FileWriteTool
from tether.tools.workspace import (
    SandboxUnavailable,
    WorkspacePolicy,
    WorkspaceViolation,
)


def test_enforced_policy_resolves_relative_paths_from_root(tmp_path: Path):
    root = tmp_path / "project"
    root.mkdir()
    policy = WorkspacePolicy(root, enforced=True)

    assert policy.resolve("src/main.py") == root / "src/main.py"
    with pytest.raises(WorkspaceViolation):
        policy.resolve("../secret.txt")


def test_enforced_policy_rejects_symlink_escape(tmp_path: Path):
    root = tmp_path / "project"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "link").symlink_to(outside, target_is_directory=True)

    with pytest.raises(WorkspaceViolation):
        WorkspacePolicy(root, enforced=True).resolve("link/file.txt")


def test_file_tools_share_workspace_denial_code(tmp_path: Path):
    root = tmp_path / "project"
    root.mkdir()
    policy = WorkspacePolicy(root, enforced=True)

    read = FileReadTool(policy).execute({"file_path": "../outside.txt"})
    write = FileWriteTool(policy).execute({"file_path": "../outside.txt", "content": "no"})

    assert read.is_error and read.error_code == "WORKSPACE_PATH_DENIED"
    assert write.is_error and write.error_code == "WORKSPACE_PATH_DENIED"
    assert not (tmp_path / "outside.txt").exists()


def test_linux_shell_fails_closed_without_bubblewrap(tmp_path: Path):
    root = tmp_path / "project"
    root.mkdir()
    policy = WorkspacePolicy(root, enforced=True)

    real_which = shutil.which

    def missing_bwrap(name: str):
        return None if name == "bwrap" else real_which(name)

    with patch.object(platform, "system", return_value="Linux"), patch.object(
        shutil, "which", side_effect=missing_bwrap
    ):
        with pytest.raises(SandboxUnavailable, match="bubblewrap"):
            policy.shell_argv("true", root)


def test_macos_profile_denies_paths_outside_all_allowed_roots(tmp_path: Path):
    root = tmp_path / "project"
    root.mkdir()
    profile = WorkspacePolicy(root, enforced=True)._macos_profile()

    assert "(allow default)" in profile
    assert "(deny file-write* (require-all" in profile
    assert f'(require-not (subpath "{root}"))' in profile


@pytest.mark.parametrize(
    "script",
    [
        "cd ~/mlxwork/bench/model && find . -type f",
        "~/mlxwork/.venv/bin/python -c 'print(1)'",
        "cat $HOME/.ssh/config",
        "cat ${HOME}/.ssh/config",
        "cat /Users/another-user/private.txt",
        "python -c \"open('/Users/another-user/private.txt')\"",
    ],
)
def test_enforced_shell_guard_rejects_explicit_workspace_escapes(
    tmp_path: Path, script: str
):
    root = tmp_path / "project"
    root.mkdir()
    policy = WorkspacePolicy(root, enforced=True)

    with pytest.raises(WorkspaceViolation, match="outside the selected workspace"):
        policy.validate_shell_script(script, root)


def test_enforced_shell_guard_rejects_parent_traversal_outside_workspace():
    root = Path(__file__).resolve().parents[1]
    policy = WorkspacePolicy(root, enforced=True)

    with pytest.raises(WorkspaceViolation, match="outside the selected workspace"):
        policy.validate_shell_script("cat ../outside.txt", root)


def test_enforced_shell_guard_allows_user_paths_inside_workspace():
    root = Path(__file__).resolve().parents[1]
    relative_root = root.relative_to(Path.home())
    policy = WorkspacePolicy(root, enforced=True)

    policy.validate_shell_script(f"cat {root / 'README.md'}", root)
    policy.validate_shell_script(f"cat $HOME/{relative_root}/README.md", root)


def test_enforced_shell_guard_allows_workspace_temp_and_system_commands(tmp_path: Path):
    root = tmp_path / "project"
    source = root / "src"
    source.mkdir(parents=True)
    policy = WorkspacePolicy(root, enforced=True)

    commands = [
        "pwd && /usr/bin/env python3 --version",
        "cat src/main.py",
        f"cat {root / 'src' / 'main.py'}",
        "printf ok > /tmp/tether-check.txt",
        f"printf ok > {Path(tempfile.gettempdir()) / 'tether-check.txt'}",
        "cat src/../README.md",
    ]
    for command in commands:
        policy.validate_shell_script(command, root)
