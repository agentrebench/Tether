"""Codex CLI backend.

This adapter is intentionally separate from the OpenAI API backend. Codex CLI
auth can use a ChatGPT/Codex login stored under ~/.codex, while the OpenAI API
backend uses API keys and API billing.
"""
from __future__ import annotations

import json
import os
import re
import select
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ..core.config import TetherConfig
from ..core.models import Message, StreamEvent, ToolDefinition


DEFAULT_CODEX_MODEL = "gpt-5.6-sol"
DEFAULT_CODEX_TIMEOUT_SEC = 900
DEFAULT_PROMPT_HISTORY_CHARS = 60_000


class CancelledByUser(Exception):
    """Raised when a streaming completion is interrupted via cancel_event."""


@dataclass(frozen=True)
class CodexCommandResult:
    ok: bool
    output: str
    returncode: int | None = None
    auth_mode: str = ""


def _codex_executable() -> str | None:
    configured = os.environ.get("TETHER_CODEX_BIN", "").strip()
    if configured:
        if os.path.sep in configured:
            path = Path(configured).expanduser()
            return str(path) if path.exists() else None
        found = shutil.which(configured)
        if found:
            return found

    found = shutil.which("codex")
    if found:
        return found

    # Desktop launchers and background services may not inherit an interactive
    # shell's nvm path. Fall back to the common Codex npm install location.
    nvm_dir = Path.home() / ".nvm" / "versions" / "node"
    candidates = sorted(
        nvm_dir.glob("*/bin/codex"),
        key=lambda p: p.stat().st_mtime if p.exists() else 0,
        reverse=True,
    )
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return None


def _codex_auth_mode(output: str) -> str:
    lower = output.lower()
    if "api key" in lower:
        return "api_key"
    if "access token" in lower:
        return "access_token"
    if "chatgpt" in lower or "chat gpt" in lower:
        return "chatgpt"
    if "logged in" in lower:
        return "unknown"
    return ""


def codex_login_status(timeout: float = 10.0, require_account: bool = True) -> CodexCommandResult:
    exe = _codex_executable()
    if not exe:
        return CodexCommandResult(False, "Codex CLI is not installed or not on PATH.")
    try:
        proc = subprocess.run(
            [exe, "login", "status"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return CodexCommandResult(False, "Timed out checking Codex login status.")
    except OSError as e:
        return CodexCommandResult(False, f"Could not run Codex CLI: {e}")

    output = _clean_terminal_output(
        "\n".join(part.strip() for part in (proc.stdout, proc.stderr) if part.strip())
    )
    auth_mode = _codex_auth_mode(output)
    logged_in = proc.returncode == 0 and "Logged in" in output
    ok = logged_in
    if require_account and logged_in and auth_mode in ("api_key", "access_token"):
        ok = False
        output = (
            f"{output}\n\n"
            "Tether OpenAI account mode requires ChatGPT/OpenAI account login, "
            "not Codex API-key auth. Run `codex logout`, then `codex login --device-auth`."
        )
    return CodexCommandResult(
        ok,
        output or f"codex login status exited {proc.returncode}",
        proc.returncode,
        auth_mode,
    )


class CodexExecBackend:
    """Adapter that turns chat-completion calls into `codex exec` invocations."""

    def __init__(self, config: TetherConfig, logger=None, cwd: str | None = None):
        self.config = config
        self.logger = logger
        self.cwd = Path(cwd or os.getcwd())

    def health_check(self) -> bool:
        return codex_login_status().ok

    def chat_completion(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        temperature: float | None = None,
        max_tokens: int = 4096,
    ) -> tuple[Message, dict]:
        exe = _codex_executable()
        if not exe:
            raise RuntimeError("Codex CLI is not installed or not on PATH.")
        status = codex_login_status(timeout=10.0)
        if not status.ok:
            raise RuntimeError(
                "Codex CLI is not logged in with a ChatGPT account. Run "
                "`codex login --device-auth`, finish authentication, and "
                "confirm `codex login status` succeeds."
            )

        prompt = self._build_prompt(messages)
        exe_path, cmd, timeout, out_path = self._build_exec_cmd(exe, json_mode=False)
        try:
            proc = subprocess.run(
                cmd,
                input=prompt,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            try:
                final_text = out_path.read_text(encoding="utf-8").strip() if out_path else ""
            except OSError:
                final_text = ""
        except subprocess.TimeoutExpired as e:
            raise RuntimeError(f"Codex exec timed out after {timeout}s") from e
        except OSError as e:
            raise RuntimeError(f"Could not run Codex exec: {e}") from e
        finally:
            if out_path:
                try:
                    out_path.unlink()
                except OSError:
                    pass

        stdout = (proc.stdout or "").strip()
        stderr = (proc.stderr or "").strip()
        if proc.returncode != 0:
            detail = "\n".join(x for x in (stdout, stderr) if x).strip()
            if len(detail) > 2500:
                detail = detail[-2500:]
            raise RuntimeError(f"Codex exec failed with exit code {proc.returncode}: {detail}")

        if not final_text:
            final_text = _fallback_last_message(stdout, stderr)
        return Message(role="assistant", content=final_text or "(no reply)"), {}

    def chat_completion_streaming(
        self,
        messages: list[Message],
        on_event: Callable[[StreamEvent], None],
        tools: list[ToolDefinition] | None = None,
        temperature: float | None = None,
        max_tokens: int = 4096,
        cancel_event: threading.Event | None = None,
    ) -> tuple[Message, dict]:
        """Stream codex events via `codex exec --json`.

        Codex emits whole-message items, not token deltas. We translate
        `command_execution` items into tool_running / tool_done events so
        the UI can show progress, and the final `agent_message` item lands
        as a single big `text` event at the end.
        """
        exe = _codex_executable()
        if not exe:
            raise RuntimeError("Codex CLI is not installed or not on PATH.")
        status = codex_login_status(timeout=10.0)
        if not status.ok:
            raise RuntimeError(
                "Codex CLI is not logged in. Run `codex login --device-auth`."
            )

        prompt = self._build_prompt(messages)
        exe_path, cmd, timeout, _ = self._build_exec_cmd(exe, json_mode=True)
        usage: dict = {}
        final_text = ""

        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
        except OSError as e:
            raise RuntimeError(f"Could not start Codex exec: {e}") from e

        assert proc.stdin is not None and proc.stdout is not None
        try:
            proc.stdin.write(prompt)
            proc.stdin.close()
        except OSError as e:
            proc.kill()
            raise RuntimeError(f"Codex exec stdin closed early: {e}") from e

        # Drain stderr concurrently: with stderr=PIPE and no reader, a chatty
        # codex process blocks once the pipe buffer (~64KB) fills, stops
        # writing stdout, and the loop below spins until the deadline.
        stderr_chunks: list[str] = []

        def _drain_stderr() -> None:
            try:
                if proc.stderr is not None:
                    for line in proc.stderr:
                        stderr_chunks.append(line)
            except (OSError, ValueError):
                pass

        stderr_thread = threading.Thread(target=_drain_stderr, daemon=True)
        stderr_thread.start()

        deadline = time.monotonic() + timeout
        fd = proc.stdout.fileno()
        buffer = b""
        try:
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    proc.kill()
                    raise CancelledByUser("codex exec cancelled")
                if time.monotonic() > deadline:
                    proc.kill()
                    raise RuntimeError(f"Codex exec timed out after {timeout}s")
                if proc.poll() is not None and not select.select([fd], [], [], 0)[0]:
                    break
                ready, _, _ = select.select([fd], [], [], 0.4)
                if not ready:
                    continue
                chunk = os.read(fd, 8192)
                if not chunk:
                    break
                buffer += chunk
                while b"\n" in buffer:
                    raw, _, buffer = buffer.partition(b"\n")
                    line = raw.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    text, evt = _codex_event_to_stream(event)
                    if evt is not None:
                        try:
                            on_event(evt)
                        except Exception:
                            if self.logger:
                                self.logger.exception("on_event callback raised")
                    if text:
                        final_text = text
                    if event.get("type") == "turn.completed":
                        usage = event.get("usage") or {}
        finally:
            try:
                proc.wait(timeout=max(2.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            stderr_thread.join(timeout=2.0)
            # Closing a TextIOWrapper takes its lock; if the drain thread is
            # still blocked in readline (a grandchild inherited the fd), that
            # close would hang this turn. Leave it to the daemon thread then.
            streams = [proc.stdout]
            if not stderr_thread.is_alive():
                streams.append(proc.stderr)
            for stream in streams:
                try:
                    if stream is not None:
                        stream.close()
                except OSError:
                    pass

        if proc.returncode not in (0, None):
            err = "".join(stderr_chunks)
            raise RuntimeError(
                f"Codex exec failed with exit code {proc.returncode}: {err[-1500:]}"
            )

        return Message(role="assistant", content=final_text or "(no reply)"), usage

    def _build_exec_cmd(self, exe: str, json_mode: bool) -> tuple[str, list[str], int, Path | None]:
        """Shared cmd builder for streaming and non-streaming exec."""
        timeout = int(os.environ.get("TETHER_CODEX_TIMEOUT", DEFAULT_CODEX_TIMEOUT_SEC))
        # The outer Tether engine owns tools and approvals. Keep the nested
        # Codex process read-only by default so it cannot bypass that policy.
        # Advanced deployments can explicitly override the sandbox.
        sandbox = os.environ.get("TETHER_CODEX_SANDBOX", "read-only")
        approval = os.environ.get("TETHER_CODEX_APPROVAL", "never")
        model = (self.config.api_model or DEFAULT_CODEX_MODEL).strip()

        cmd = [
            exe,
            "-a",
            approval,
            "exec",
            "--ephemeral",
            "--color",
            "never",
            "--skip-git-repo-check",
            "-C",
            str(self.cwd),
            "-s",
            sandbox,
        ]
        out_path: Path | None = None
        if json_mode:
            cmd.append("--json")
        else:
            fd, out_name = tempfile.mkstemp(prefix="tether-codex-", suffix=".txt")
            os.close(fd)
            out_path = Path(out_name)
            cmd.extend(["-o", str(out_path)])
        if model:
            cmd.extend(["-m", model])
        # A transient override (plan mode) wins over the configured effort,
        # exactly like the HTTP backend; Codex takes it as a config override.
        effort = (getattr(self, "reasoning_effort_override", "") or self.config.reasoning_effort or "").strip()
        if effort:
            cmd.extend(["-c", f'model_reasoning_effort="{effort}"'])
        cmd.append("-")
        return exe, cmd, timeout, out_path

    @staticmethod
    def _build_prompt(messages: list[Message]) -> str:
        max_chars = int(os.environ.get("TETHER_CODEX_PROMPT_CHARS", DEFAULT_PROMPT_HISTORY_CHARS))
        blocks: list[str] = [
            "You are the language-model backend for the Tether developer agent.",
            "Use only the supplied conversation. Do not run commands, inspect files, "
            "or modify the workspace; the outer Tether engine owns tools, policy, "
            "and user approvals.",
            "Return the assistant response only. Markdown is allowed.",
            "",
            "Conversation:",
        ]

        for msg in messages:
            blocks.append(_format_message_for_prompt(msg))

        prompt = "\n\n".join(block for block in blocks if block).strip()
        if len(prompt) <= max_chars:
            return prompt
        head = "\n\n".join(blocks[:5])
        tail_budget = max(1000, max_chars - len(head) - 80)
        return f"{head}\n\n[Earlier conversation truncated]\n\n{prompt[-tail_budget:]}"


def _format_message_for_prompt(msg: Message) -> str:
    role = msg.role.upper()
    if msg.role == "tool":
        role = f"TOOL RESULT {msg.name or ''}".strip().upper()
    content = (msg.content or "").strip()
    if not content and msg.tool_calls:
        names = ", ".join(tc.name for tc in msg.tool_calls)
        content = f"[assistant requested tool call(s): {names}]"
    if len(content) > 8000:
        content = content[:4000] + "\n...[message truncated]...\n" + content[-3000:]
    return f"{role}:\n{content}"


def _codex_event_to_stream(event: dict) -> tuple[str, StreamEvent | None]:
    """Translate one codex --json event into a StreamEvent.

    Returns (final_agent_text, event). final_agent_text is non-empty only
    when this event is a completed `agent_message` item carrying the reply.
    """
    et = event.get("type", "")
    if et == "item.started":
        item = event.get("item") or {}
        if item.get("type") == "command_execution":
            cmd = (item.get("command") or "").strip()
            preview = cmd if len(cmd) <= 200 else cmd[:200] + "…"
            return "", StreamEvent(type="tool_running", tool_name="bash", tool_args_preview=preview)
        return "", None
    if et == "item.completed":
        item = event.get("item") or {}
        itype = item.get("type")
        if itype == "command_execution":
            cmd = (item.get("command") or "").strip()
            exit_code = item.get("exit_code")
            output = (item.get("aggregated_output") or "").strip()
            tail = output[-180:] if len(output) > 180 else output
            return "", StreamEvent(
                type="tool_done",
                tool_name="bash",
                tool_args_preview=cmd[:120],
                tool_output_preview=tail,
                is_error=bool(exit_code) and exit_code != 0,
            )
        if itype == "agent_message":
            text = (item.get("text") or "").strip()
            return text, StreamEvent(type="text", text=text)
    if et == "turn.completed":
        return "", StreamEvent(type="complete")
    return "", None


def _fallback_last_message(stdout: str, stderr: str) -> str:
    combined = "\n".join(x for x in (stdout, stderr) if x).strip()
    if not combined:
        return ""
    lines = [line.strip() for line in combined.splitlines() if line.strip()]
    for marker in ("final answer", "assistant"):
        for idx, line in enumerate(lines):
            if marker in line.lower() and idx + 1 < len(lines):
                return "\n".join(lines[idx + 1:]).strip()
    return lines[-1]


_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_OSC_RE = re.compile(r"\x1b\].*?(?:\x07|\x1b\\)")


def _clean_terminal_output(text: str) -> str:
    text = _OSC_RE.sub("", text)
    text = _ANSI_RE.sub("", text)
    text = text.replace("\r", "\n")
    text = "".join(ch for ch in text if ch == "\n" or ch == "\t" or ord(ch) >= 32)
    lines = [line.rstrip() for line in text.splitlines()]
    compact: list[str] = []
    for line in lines:
        if line or (compact and compact[-1]):
            compact.append(line)
    return "\n".join(compact).strip()
