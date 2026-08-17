"""Bash tool — executes shell commands with persistent working directory."""
from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
import uuid

from ..core.models import ToolResult
from ..core.logging import get_logger
from .base import BaseTool
from .jobs import JobRegistry
from .workspace import SandboxUnavailable, WorkspacePolicy, WorkspaceViolation


class BashTool(BaseTool):
    def __init__(
        self,
        workspace: WorkspacePolicy | None = None,
        jobs: JobRegistry | None = None,
    ):
        self.workspace = workspace or WorkspacePolicy.unrestricted()
        self.jobs = jobs or JobRegistry()
        self._default_cwd = str(self.workspace.root or os.getcwd())
        self._local = threading.local()
        self.logger = get_logger(__name__)

    def _get_cwd(self) -> str:
        return getattr(self._local, "cwd", self._default_cwd)

    def _set_cwd(self, cwd: str) -> None:
        self._local.cwd = cwd

    def set_cwd(self, cwd: str) -> None:
        """Reset the current thread's shell cwd and future default cwd."""
        self._default_cwd = cwd
        self._set_cwd(cwd)

    @property
    def name(self) -> str:
        return "bash"

    @property
    def description(self) -> str:
        return "Execute a bash command and return its output. Working directory persists between calls on the same thread, so concurrent agents keep isolated shells."

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The bash command to execute",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Timeout in seconds (default 120)",
                    "default": 120,
                },
                "run_in_background": {
                    "type": "boolean",
                    "description": "Start the command as a background job and return a job id immediately.",
                    "default": False,
                },
            },
            "required": ["command"],
        }

    @staticmethod
    def _terminate_group(proc: subprocess.Popen) -> None:
        """SIGTERM the command's whole process group, escalating to SIGKILL.

        The shell runs in its own session (start_new_session=True), so the
        group id is the shell's pid and the signal reaches any children the
        command spawned, not just bash itself.
        """
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

    def execute(self, arguments: dict) -> ToolResult:
        return self.execute_with_cancel(arguments)

    def execute_with_cancel(self, arguments: dict, cancel_event=None) -> ToolResult:
        command = arguments.get("command", "")
        try:
            timeout = float(arguments.get("timeout") or 120)
        except (TypeError, ValueError):
            timeout = 120.0
        if timeout.is_integer():
            timeout = int(timeout)
        run_in_background = bool(arguments.get("run_in_background", False))

        if not command:
            self.logger.warning("Bash command executed with empty command")
            return ToolResult(
                tool_call_id="",
                name=self.name,
                content="Error: no command provided",
                is_error=True,
            )

        self.logger.debug(f"Executing bash command: {command[:100]}{'...' if len(command) > 100 else ''}")
        
        # Wrap command to capture the final working directory with a unique marker
        marker = f"__TETHER_CWD__:{uuid.uuid4().hex}:"
        wrapped = (
            f"{{\n{command}\n}}\n"
            "status=$?\n"
            f'printf "\\n{marker}%s\\n" "$PWD"\n'
            "exit $status"
        )
        cwd = self._get_cwd()

        try:
            if run_in_background:
                argv = self.workspace.shell_argv(command, cwd)
                snapshot = self.jobs.start_process(
                    kind="shell",
                    label=command[:160],
                    argv=argv,
                    cwd=str(self.workspace.resolve(cwd, label="working directory")),
                    timeout=(
                        int(timeout)
                        if "timeout" in arguments and timeout
                        else None
                    ),
                )
                return ToolResult(
                    name=self.name,
                    content=(
                        f"Started background job {snapshot['id']}. "
                        "Use job_output to read output or job_kill to stop it."
                    ),
                    display_kind="terminal",
                    metadata={"job": snapshot},
                )
            argv = self.workspace.shell_argv(wrapped, cwd)
            proc = subprocess.Popen(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=cwd,
                start_new_session=True,
            )
            deadline = time.monotonic() + timeout
            while True:
                try:
                    out, err = proc.communicate(timeout=0.2)
                    break
                except subprocess.TimeoutExpired:
                    if cancel_event is not None and cancel_event.is_set():
                        self.logger.warning(
                            f"Bash command cancelled by user: {command[:50]}{'...' if len(command) > 50 else ''}"
                        )
                        self._terminate_group(proc)
                        out, _ = proc.communicate()
                        tail = (out or "").strip()[-500:]
                        content = "Command cancelled by user before completion."
                        if tail:
                            content += f"\nPartial output:\n{tail}"
                        return ToolResult(
                            tool_call_id="",
                            name=self.name,
                            content=content,
                            is_error=True,
                        )
                    if time.monotonic() >= deadline:
                        self._terminate_group(proc)
                        proc.communicate()
                        raise subprocess.TimeoutExpired(command, timeout)

            result = subprocess.CompletedProcess(
                args=command, returncode=proc.returncode, stdout=out, stderr=err
            )

            stdout = result.stdout
            # Extract and update working directory for this thread from the final marker line only.
            stdout_lines = stdout.splitlines()
            if stdout_lines and stdout_lines[-1].startswith(marker):
                new_cwd = stdout_lines[-1][len(marker):]
                try:
                    resolved_cwd = self.workspace.resolve(new_cwd, label="working directory")
                except WorkspaceViolation:
                    resolved_cwd = None
                if resolved_cwd is not None and resolved_cwd.is_dir():
                    self._set_cwd(str(resolved_cwd))
                stdout = "\n".join(stdout_lines[:-1]).rstrip("\n")

            output = stdout
            if result.stderr:
                output += f"\nSTDERR:\n{result.stderr}"
            if result.returncode != 0:
                output += f"\n(exit code: {result.returncode})"

            output += f"\n[cwd: {self._get_cwd()}]"
            
            if result.returncode == 0:
                self.logger.debug(f"Bash command succeeded: {command[:50]}{'...' if len(command) > 50 else ''}")
            else:
                self.logger.warning(f"Bash command failed with exit code {result.returncode}: {command[:50]}{'...' if len(command) > 50 else ''}")

            return ToolResult(
                tool_call_id="",
                name=self.name,
                content=output.strip() or "(no output)",
                is_error=result.returncode != 0,
                display_kind="terminal",
                error_code="COMMAND_FAILED" if result.returncode != 0 else "",
                metadata={"cwd": self._get_cwd(), "exit_code": result.returncode},
            )
        except WorkspaceViolation as e:
            return ToolResult(
                name=self.name, content=f"Workspace path denied: {e}",
                is_error=True, error_code="WORKSPACE_PATH_DENIED",
                display_kind="terminal",
            )
        except SandboxUnavailable as e:
            return ToolResult(
                name=self.name, content=str(e), is_error=True,
                error_code="SANDBOX_UNAVAILABLE", display_kind="terminal",
            )
        except subprocess.TimeoutExpired:
            self.logger.error(f"Bash command timed out after {timeout}s: {command[:50]}{'...' if len(command) > 50 else ''}")
            return ToolResult(
                tool_call_id="",
                name=self.name,
                content=f"Command timed out after {timeout}s",
                is_error=True,
            )
        except Exception as e:
            self.logger.error(f"Unexpected error executing bash command: {e}", exc_info=True)
            return ToolResult(
                tool_call_id="",
                name=self.name,
                content=f"Unexpected error: {e}",
                is_error=True,
            )
