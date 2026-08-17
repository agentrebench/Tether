"""Agent tool — allows the model to spawn sub-agents, with parallel execution."""
from __future__ import annotations

import concurrent.futures
from dataclasses import dataclass, field
import itertools
import re
import sys
import threading
import time
from typing import Callable

from ..agents.base import AgentResult, AgentToolEvent, SubAgent
from ..agents.builtin import BUILTIN_AGENTS
from ..core.config import TetherConfig
from ..core.models import ToolResult
from ..core.permissions import PermissionContext
from ..ui.colors import DIM, GREEN, RED, RESET, agent_label, dim
from .base import BaseTool, ToolRegistry


_SAFE_ARGUMENT_KEYS: dict[str, tuple[str, ...]] = {
    "agent": ("agent_type", "count"),
    "bash": ("command", "timeout", "run_in_background"),
    "file_edit": (
        "file_path", "start_line", "end_line", "insert_before_line",
        "insert_after_line", "append", "replace_all", "confidence",
    ),
    "file_read": ("file_path", "offset", "limit"),
    "file_write": ("file_path",),
    "glob": ("pattern", "path"),
    "grep": (
        "pattern", "path", "glob", "context", "before_context",
        "after_context", "case_insensitive",
    ),
    "job_kill": ("job_id",),
    "job_output": ("job_id", "tail_chars"),
    "lsp": ("operation", "file_path", "line", "character"),
    "model_check": ("changed_files",),
    "model_query": ("action", "query", "path"),
    "skill_manage": ("action", "name"),
    "skill_view": ("name", "path"),
    "web_fetch": ("url", "max_chars", "timeout"),
}
_SENSITIVE_ARGUMENT_NAMES = re.compile(
    r"(?:api[_-]?key|authorization|cookie|credential|password|passwd|secret|token)",
    re.IGNORECASE,
)
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b([A-Z0-9_]*(?:API[_-]?KEY|AUTHORIZATION|COOKIE|CREDENTIAL|"
    r"PASSWORD|PASSWD|SECRET|TOKEN)[A-Z0-9_]*)\s*([:=])\s*"
    r"(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)
_BEARER_TOKEN = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}")
_SECRET_CLI_FLAG = re.compile(
    r"(?i)(--?(?:api[-_]?key|authorization|cookie|credential|password|passwd|"
    r"secret|token))(?:\s+|=)(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)
_COMMON_SECRET_TOKEN = re.compile(
    r"\b(?:sk-[A-Za-z0-9_-]{8,}|gh[pousr]_[A-Za-z0-9_-]{8,}|"
    r"xox[baprs]-[A-Za-z0-9_-]{8,})\b",
    re.IGNORECASE,
)
_PRIVATE_KEY_BLOCK = re.compile(
    r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----",
    re.DOTALL,
)


def _redact_secrets(value: object) -> str:
    text = str(value)
    text = _PRIVATE_KEY_BLOCK.sub("[REDACTED PRIVATE KEY]", text)
    text = _BEARER_TOKEN.sub("Bearer [REDACTED]", text)
    text = _SECRET_CLI_FLAG.sub(lambda match: f"{match.group(1)} [REDACTED]", text)
    text = _SECRET_ASSIGNMENT.sub(lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]", text)
    return _COMMON_SECRET_TOKEN.sub("[REDACTED]", text)


def _clip_safe_text(value: object, limit: int) -> str:
    text = _redact_secrets(value).strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def safe_tool_args_preview(tool_name: str, arguments: dict) -> str:
    """Return a compact allowlisted preview suitable for user-visible events."""
    if not isinstance(arguments, dict) or not arguments:
        return ""
    keys = _SAFE_ARGUMENT_KEYS.get(tool_name, ())
    parts: list[str] = []
    for key in keys:
        if key not in arguments or _SENSITIVE_ARGUMENT_NAMES.search(key):
            continue
        value = arguments[key]
        if isinstance(value, (dict, list, tuple, set)):
            rendered = f"{len(value)} item{'s' if len(value) != 1 else ''}"
        else:
            rendered = _clip_safe_text(value, 180 if key == "command" else 100)
        parts.append(f"{key}={rendered}")
    if parts:
        return _clip_safe_text(", ".join(parts), 280)
    return "(arguments hidden)"


@dataclass
class _AgentActivityState:
    agent_id: str
    agent_number: int
    agent_index: int
    label: str
    task: str
    agent_type: str
    revision: int = 0
    status: str = "queued"
    activity: str = "Waiting for a worker slot"
    tool_calls: list[dict] = field(default_factory=list)
    output: str = ""
    tokens: int = 0
    elapsed_seconds: float = 0.0
    started_at: float | None = None
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


class AgentTool(BaseTool):
    def __init__(
        self,
        config: TetherConfig,
        tool_registry: ToolRegistry,
        permissions: PermissionContext,
        on_agent_update: Callable[[dict], None] | None = None,
        on_approval_request: Callable[[str, dict], bool | str] | None = None,
    ):
        self._config = config
        self._tool_registry = tool_registry
        self._permissions = permissions
        self._max_agents = max(1, int(config.parallel_slots))
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=self._max_agents,
            thread_name_prefix="tether-agent",
        )
        self._on_agent_update = on_agent_update
        self._on_approval_request = on_approval_request
        self._approval_lock = threading.Lock()
        self._close_lock = threading.Lock()
        self._closed = False
        self._print_lock = threading.Lock()
        # Monotonic, process-wide source of unique agent display numbers. The
        # batch index passed to _run_single is only a result-ordering slot and
        # is always 1 when the engine fans agents out as separate tool calls —
        # so every such agent would otherwise render as "agent 1". This counter
        # guarantees a distinct label per launched agent regardless of path.
        # next(itertools.count) is atomic in CPython, but we lock to stay correct
        # under any implementation.
        self._label_counter = itertools.count(1)
        self._label_lock = threading.Lock()
        if self.close not in (tool_registry.close_callbacks or []):
            if tool_registry.close_callbacks is None:
                tool_registry.close_callbacks = []
            tool_registry.close_callbacks.append(self.close)

    @property
    def name(self) -> str:
        return "agent"

    @property
    def description(self) -> str:
        agents_desc = "\n".join(
            f"- {name}: {defn.description}"
            for name, defn in BUILTIN_AGENTS.items()
        )
        return (
            f"Launch a sub-agent to handle a complex task. Available agents:\n{agents_desc}\n\n"
            f"Only use this when the user explicitly asks for agents, or when the work is large enough "
            f"that parallel or isolated execution is clearly helpful. Do not use it for simple edits or small tasks.\n\n"
            f"You can run one agent, repeat the same task with count, or provide an explicit tasks list "
            f"to fan work out across multiple agents. Agents run on worker threads with live status updates. "
            f"Maximum concurrent agents: {self._config.parallel_slots}."
        )

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "agent_type": {
                    "type": "string",
                    "enum": list(BUILTIN_AGENTS.keys()),
                    "description": "Which agent to spawn",
                },
                "task": {
                    "type": "string",
                    "description": "The task description for the agent",
                },
                "count": {
                    "type": "integer",
                    "description": "Number of agents to launch for the same task",
                    "default": 1,
                    "minimum": 1,
                    "maximum": self._max_agents,
                },
                "tasks": {
                    "type": "array",
                    "description": "Optional per-agent tasks. If provided, one agent is launched per entry.",
                    "items": {"type": "string"},
                    "maxItems": self._max_agents,
                },
            },
            "required": ["agent_type", "task"],
        }

    def execute(self, arguments: dict) -> ToolResult:
        return self.execute_with_cancel(arguments)

    def execute_with_cancel(self, arguments: dict, cancel_event=None) -> ToolResult:
        agent_type = arguments.get("agent_type", "general")
        task = str(arguments.get("task", ""))
        try:
            count = max(1, int(arguments.get("count", 1)))
        except (TypeError, ValueError):
            return self._request_error("Agent count must be an integer.")

        raw_tasks = arguments.get("tasks", [])
        if raw_tasks is None:
            raw_tasks = []
        if not isinstance(raw_tasks, list):
            return self._request_error("Agent tasks must be a list of task strings.")
        tasks = [str(item) for item in raw_tasks if str(item).strip()]

        definition = BUILTIN_AGENTS.get(agent_type)
        if not definition:
            return ToolResult(
                tool_call_id="", name=self.name,
                content=f"Unknown agent type: {agent_type}. Available: {list(BUILTIN_AGENTS.keys())}",
                is_error=True,
            )

        requested_total = len(tasks) if tasks else count
        if requested_total > self._max_agents:
            return self._request_error(
                f"Requested {requested_total} agents, but this session allows at most "
                f"{self._max_agents} work item(s) per agent call."
            )

        if tasks:
            work_items = tasks
        else:
            work_items = [task for _ in range(count)]

        total = len(work_items)
        with self._print_lock:
            summary = f"{definition.name} x{total}" if total > 1 else definition.name
            sys.stdout.write(f"\n  {agent_label(summary)} {dim(_clip_safe_text(task, 100))}\n")
            sys.stdout.flush()

        states = [
            self._new_activity(agent_type, item, idx)
            for idx, item in enumerate(work_items, start=1)
        ]
        for state in states:
            self._update_agent(state)

        if total == 1:
            result = self._run_single(definition, states[0], cancel_event)
            return ToolResult(
                tool_call_id="",
                name=self.name,
                content=self._render_single_result(result),
                is_error=bool(result.error) or result.status == "approval_denied",
                error_code=result.error_code if result.status == "approval_denied" else "",
            )

        ordered_results: list[AgentResult | None] = [None] * total
        futures = []
        for state in states:
            futures.append(self._executor.submit(self._run_single, definition, state, cancel_event))

        for future in concurrent.futures.as_completed(futures):
            result = future.result()
            ordered_results[result.agent_index - 1] = result

        results = [result for result in ordered_results if result is not None]
        content = self._render_batch_results(definition.name, results)
        return ToolResult(
            tool_call_id="",
            name=self.name,
            content=content,
            is_error=any(result.error or result.status == "approval_denied" for result in results),
            error_code=(
                "APPROVAL_DENIED"
                if any(result.error_code == "APPROVAL_DENIED" for result in results)
                else (
                    "APPROVAL_DENIED_WITH_FEEDBACK"
                    if any(
                        result.error_code == "APPROVAL_DENIED_WITH_FEEDBACK"
                        for result in results
                    )
                    else ""
                )
            ),
        )

    def execute_batch(self, tasks: list[dict]) -> list[ToolResult]:
        """Run multiple agent tasks in parallel."""
        if len(tasks) > self._max_agents:
            error = self._request_error(
                f"Requested {len(tasks)} agent calls, but this session allows at most "
                f"{self._max_agents} work item(s) per batch."
            )
            return [error]
        futures = []
        for task_args in tasks:
            future = self._executor.submit(self.execute, task_args)
            futures.append(future)

        results = []
        for future in concurrent.futures.as_completed(futures):
            results.append(future.result())
        return results

    def close(self) -> None:
        """Stop accepting queued agent work during engine shutdown."""
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
        self._executor.shutdown(wait=False, cancel_futures=True)

    def _next_identity(self) -> tuple[str, int, str]:
        with self._label_lock:
            number = next(self._label_counter)
        return f"agent-{number}", number, f"agent {number}"

    def _new_activity(self, agent_type: str, task: str, index: int) -> _AgentActivityState:
        agent_id, number, label = self._next_identity()
        return _AgentActivityState(
            agent_id=agent_id,
            agent_number=number,
            agent_index=index,
            label=label,
            task=task,
            agent_type=agent_type,
        )

    def _run_single(self, definition, state: _AgentActivityState, cancel_event=None) -> AgentResult:
        started = time.monotonic()
        with state.lock:
            state.started_at = started
        self._update_agent(state, status="running", activity="Working on the assigned task")

        agent: SubAgent | None = None

        def on_tool_event(event: AgentToolEvent) -> None:
            tokens = agent.usage.total if agent is not None else 0
            self._record_tool_event(state, event, tokens=tokens)

        def on_status(label: str, status: str, detail: str) -> None:
            self._on_status(label, status, detail)

        try:
            agent = SubAgent(
                definition=definition,
                config=self._config,
                tool_registry=self._tool_registry,
                permissions=self._permissions,
                agent_index=state.agent_index,
                display_label=state.label,
                on_tool_call=self._on_tool_call,
                on_tool_event=on_tool_event,
                on_status=on_status,
                on_approval_request=(
                    (
                        lambda name, args: self._request_child_approval(
                            name, args, cancel_event
                        )
                    )
                    if self._on_approval_request is not None
                    else None
                ),
                cancel_event=cancel_event,
            )
            result = agent.run(state.task)
        except Exception as exc:
            result = AgentResult(
                agent_name=definition.name,
                agent_index=state.agent_index,
                display_label=state.label,
                output="Agent failed before completing the task.",
                task=state.task,
                status="failed",
                error=str(exc),
            )

        elapsed = time.monotonic() - started
        status = (
            result.status
            if result.status in {"completed", "failed", "cancelled", "approval_denied", "max_turns"}
            else "failed"
        )
        if result.error:
            status = "failed"
        activity = {
            "completed": "Completed the assigned task",
            "failed": "Failed before completing the task",
            "cancelled": "Cancelled by the user",
            "approval_denied": "Stopped because approval was denied",
            "max_turns": "Stopped at the turn limit (partial output kept)",
        }[status]
        callback_output = (
            result.output
            if status in {"completed", "cancelled", "approval_denied", "max_turns"}
            else "Agent failed before completing the task."
        )
        self._update_agent(
            state,
            status=status,
            activity=activity,
            output=callback_output,
            tokens=result.usage.total,
            elapsed_seconds=elapsed,
        )

        if status == "completed":
            n_tools = len(result.tool_calls_made)
            tok = result.usage.total
            tok_s = f"{tok / 1000:.1f}k" if tok >= 1000 else str(tok)
            with self._print_lock:
                sys.stdout.write(
                    f"    {GREEN}✓{RESET} {agent_label(result.display_label)} "
                    f"{DIM}{n_tools} tool{'s' if n_tools != 1 else ''} · "
                    f"{tok_s} tok · {elapsed:.0f}s{RESET}\n")
                sys.stdout.flush()
        return result

    def _request_child_approval(self, name: str, arguments: dict, cancel_event=None) -> bool | str:
        while not self._approval_lock.acquire(timeout=0.1):
            if cancel_event is not None and cancel_event.is_set():
                return False
        try:
            if cancel_event is not None and cancel_event.is_set():
                return False
            try:
                return self._on_approval_request(name, arguments) if self._on_approval_request else False
            except Exception:
                return False
        finally:
            self._approval_lock.release()

    def _update_agent(self, state: _AgentActivityState, **changes) -> None:
        with state.lock:
            for key, value in changes.items():
                setattr(state, key, value)
            if state.started_at is not None and "elapsed_seconds" not in changes:
                state.elapsed_seconds = time.monotonic() - state.started_at
            state.revision += 1
            snapshot = self._snapshot(state)
        self._deliver_agent_update(snapshot)

    def _record_tool_event(
        self,
        state: _AgentActivityState,
        event: AgentToolEvent,
        *,
        tokens: int,
    ) -> None:
        label = event.tool_name.replace("_", " ")
        with state.lock:
            record_id = (
                f"{state.agent_id}:{event.tool_call_id}"
                if event.tool_call_id
                else ""
            )
            index = next(
                (
                    idx for idx in range(len(state.tool_calls) - 1, -1, -1)
                    if (
                        (record_id and state.tool_calls[idx]["id"] == record_id)
                        or (
                            not record_id
                            and state.tool_calls[idx]["name"] == event.tool_name
                            and state.tool_calls[idx]["status"] == "running"
                        )
                    )
                ),
                -1,
            )
            if event.status == "running" and index < 0:
                if not record_id:
                    record_id = f"{state.agent_id}:tool-{len(state.tool_calls) + 1}"
                state.tool_calls.append({
                    "id": record_id,
                    "name": event.tool_name,
                    "status": "running",
                    "args_preview": safe_tool_args_preview(event.tool_name, event.arguments),
                    "is_error": False,
                    "error_code": "",
                })
            elif index >= 0:
                state.tool_calls[index] = {
                    **state.tool_calls[index],
                    "status": event.status,
                    "is_error": event.is_error,
                    "error_code": _clip_safe_text(event.error_code, 80),
                }
            else:
                state.tool_calls.append({
                    "id": record_id or f"{state.agent_id}:tool-{len(state.tool_calls) + 1}",
                    "name": event.tool_name,
                    "status": event.status,
                    "args_preview": safe_tool_args_preview(event.tool_name, event.arguments),
                    "is_error": event.is_error,
                    "error_code": _clip_safe_text(event.error_code, 80),
                })
            state.tokens = tokens
            state.elapsed_seconds = (
                time.monotonic() - state.started_at if state.started_at is not None else 0.0
            )
            state.activity = (
                f"Running {label}"
                if event.status == "running"
                else f"{label.capitalize()} {event.status}"
            )
            state.revision += 1
            snapshot = self._snapshot(state)
        self._deliver_agent_update(snapshot)

    @staticmethod
    def _snapshot(state: _AgentActivityState) -> dict:
        return {
            "agent_id": state.agent_id,
            "agent_number": state.agent_number,
            "revision": state.revision,
            "label": state.label,
            "task": _clip_safe_text(state.task, 2_000),
            "agent_type": state.agent_type,
            "status": state.status,
            "activity": state.activity,
            "tool_calls": [dict(item) for item in state.tool_calls],
            "output": _clip_safe_text(state.output, 12_000),
            "tokens": max(0, int(state.tokens)),
            "elapsed_seconds": round(max(0.0, float(state.elapsed_seconds)), 3),
        }

    def _deliver_agent_update(self, snapshot: dict) -> None:
        if self._on_agent_update is None:
            return
        try:
            self._on_agent_update(snapshot)
        except Exception:
            pass

    def _request_error(self, message: str) -> ToolResult:
        return ToolResult(
            tool_call_id="",
            name=self.name,
            content=message,
            is_error=True,
        )

    def _on_tool_call(self, label: str, name: str, arguments: dict) -> None:
        preview = safe_tool_args_preview(name, arguments)
        if len(preview) > 70:
            preview = preview[:67] + "..."
        with self._print_lock:
            sys.stdout.write(f"    {agent_label(label)} {DIM}▸{RESET} {name}")
            if preview:
                sys.stdout.write(f" {DIM}{preview}{RESET}")
            sys.stdout.write("\n")
            sys.stdout.flush()

    def _on_status(self, label: str, status: str, detail: str) -> None:
        # Per-turn "thinking" ticks and the completion echo are noise — the
        # engine spinner shows liveness and _run_single prints the ✓ summary.
        # Only the start of work and failures earn a line.
        if status == "running":
            glyph, color = "…", DIM
        elif status == "cancelled":
            glyph, color = "■", DIM
        elif status == "approval_denied":
            glyph, color = "■", RED
        elif status == "failed":
            glyph, color = "✗", RED
        else:
            return
        preview = _clip_safe_text(detail, 90).replace("\n", " ")
        if len(preview) > 90:
            preview = preview[:87] + "..."
        with self._print_lock:
            sys.stdout.write(f"    {color}{glyph}{RESET} {agent_label(label)}")
            if preview:
                sys.stdout.write(f" {DIM}{preview}{RESET}")
            sys.stdout.write("\n")
            sys.stdout.flush()

    @staticmethod
    def _render_single_result(result: AgentResult) -> str:
        header = f"[Agent '{result.agent_name}' completed]"
        if result.status == "cancelled":
            header = f"[Agent '{result.agent_name}' cancelled]"
        elif result.status == "approval_denied":
            header = f"[Agent '{result.agent_name}' stopped: approval denied]"
        elif result.status == "max_turns":
            header = f"[Agent '{result.agent_name}' stopped: turn limit reached]"
        elif result.error or result.status == "failed":
            header = f"[Agent '{result.agent_name}' failed]"
        safe_task = _clip_safe_text(result.task, 2_000)
        task_line = f"Task: {safe_task}" if safe_task else "Task: (not provided)"
        return f"{header}\n{task_line}\n{_clip_safe_text(result.output, 12_000)}"

    @staticmethod
    def _render_batch_results(agent_name: str, results: list[AgentResult]) -> str:
        lines = [f"[Launched {len(results)} {agent_name} agents]"]
        for result in results:
            status = (
                result.status
                if result.status in {"completed", "failed", "cancelled", "approval_denied", "max_turns"}
                else "failed"
            )
            tool_count = len(result.tool_calls_made)
            label = result.display_label or f"agent {result.agent_index}"
            lines.append(
                f"{label}: {status}, {tool_count} tool call(s), {result.usage.total} tokens"
            )
            safe_task = _clip_safe_text(result.task, 2_000)
            if safe_task:
                lines.append(f"Task: {safe_task}")
            body = _clip_safe_text(result.output, 12_000) or "(no output)"
            lines.append(AgentTool._clip_output(body))
            lines.append("")
        return "\n".join(lines).strip()

    @staticmethod
    def _clip_output(text: str, limit: int = 2500) -> str:
        if len(text) <= limit:
            return text
        return text[:limit] + f"\n... ({len(text) - limit} more characters)"
