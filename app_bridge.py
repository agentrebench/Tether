"""Structured bridge used by the native Tether desktop app.

The terminal REPL is deliberately presentation-heavy: it streams ANSI output,
owns stdin, and uses prompt_toolkit.  A GUI should not scrape that interface.
This module keeps QueryEngine as the single implementation of agent behavior
and exposes a small newline-delimited JSON protocol over stdin/stdout instead.

The bridge is internal for now.  Start it with::

    tether app-bridge --project /path/to/repository
"""
from __future__ import annotations

import argparse
from collections import deque
import contextlib
import hashlib
import io
import json
import os
import re
import sys
import threading
import uuid
import copy
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO

from . import __version__
from .core.config import (
    CONFIG_DIR,
    REMOTE_PROVIDERS,
    TetherConfig,
    apply_provider_selection,
)
from .core.models import StreamEvent
from .core.permissions import APPROVAL_DENIED_SIGNAL, PermissionContext
from .engine.query_engine import QueryEngine
from .engine.turn_controller import mark_interactive
from .tools.agent_tool import AgentTool
from .tools.ask_user import AskUserTool
from .tools.base import ToolRegistry
from .tools.todo import TodoStore, TodoWriteTool


_ANSI_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
_ENV_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

_APP_COMMAND_SPECS = (
    ("/help", "Show all commands", "conversation"),
    ("/plan", "Toggle plan mode", "conversation"),
    ("/plan on", "Force plan on", "conversation"),
    ("/plan off", "Force plan off", "conversation"),
    ("/why", "Reasoning for an edited line", "conversation"),
    ("/clear", "Clear the context", "conversation"),
    ("/compact", "Summarize + trim context", "conversation"),
    ("/history", "Event history", "conversation"),
    ("/usage", "Token usage", "session"),
    ("/context", "Context-window usage", "session"),
    ("/session", "Session info", "session"),
    ("/cd", "Change directory", "session"),
    ("/save", "Save session to disk", "session"),
    ("/resume", "Restore a saved conversation", "session"),
    ("/memory", "Persistent memory", "memory"),
    ("/memory show", "Show memory", "memory"),
    ("/memory clear", "Clear memory", "memory"),
    ("/selfcheck", "Run a self-check", "verification"),
    ("/summaries", "Conversation summaries", "session"),
    ("/provider", "Switch model provider", "runtime"),
    ("/model", "LLM model & token usage", "runtime"),
    ("/model stats", "Show the current model", "runtime"),
    ("/model local", "Switch to local GGUF", "runtime"),
    ("/model api", "Switch to your API model", "runtime"),
    ("/learn", "Author a skill", "skills"),
    ("/learn auto", "Toggle auto-learning", "skills"),
    ("/learn this chat", "Skill from this chat", "skills"),
    ("/skills", "List / show / forget skills", "skills"),
    ("/skills show", "Show a skill", "skills"),
    ("/skills forget", "Delete a user skill", "skills"),
    ("/skills doctor", "Check skill files for problems", "skills"),
    ("/persistence", "Codebase mental model", "mental model"),
    ("/persistence status", "Model status", "mental model"),
    ("/persistence build", "Full re-index", "mental model"),
    ("/persistence sync", "Incremental refresh", "mental model"),
    ("/persistence check", "Run invariants vs. diff", "mental model"),
    ("/persistence ask", "Query the model", "mental model"),
    ("/persistence gc", "Remove orphaned model DBs", "mental model"),
    ("/also", "Queue a follow-up mid-turn", "control"),
    ("/redirect", "Steer the current turn", "control"),
    ("/stop", "Interrupt the turn", "control"),
    ("/quit", "Exit", "control"),
    ("/exit", "Exit", "control"),
)


def normalize_api_key(value: str) -> str:
    """Validate one raw HTTP credential without ever logging it."""
    key = value.strip()
    if not key:
        return ""
    if any(ord(char) < 0x21 or ord(char) > 0x7E for char in key):
        raise ValueError("API keys must contain printable characters without spaces.")
    if _ENV_ASSIGNMENT_RE.match(key):
        raise ValueError("Paste only the API key value, not NAME=value.")
    if len(key) >= 2 and key[0] == key[-1] and key[0] in {'\"', "'"}:
        raise ValueError("Paste the API key without surrounding quotes.")
    return key


def clean_terminal_output(value: str, limit: int = 12_000) -> str:
    """Return a compact, escape-free diagnostic transcript."""
    cleaned = _ANSI_RE.sub("", value)
    cleaned = re.sub(
        r"[\r\n]*[^\r\n]*(?:Thinking|Reasoning)\.\.\.[^\r\n]*[\r\n]*",
        "\n",
        cleaned,
    ).replace("\r", "\n")
    lines = [line.rstrip() for line in cleaned.splitlines()]
    compact: list[str] = []
    for line in lines:
        # Spinner repaint frames are useful in a terminal and noise everywhere
        # else. Keep real activity lines and collapse repeated blank lines.
        if "Thinking..." in line or "Reasoning..." in line:
            continue
        if not line and (not compact or not compact[-1]):
            continue
        compact.append(line)
    result = "\n".join(compact).strip()
    result = re.sub(r"\n{3,}", "\n\n", result)
    if len(result) > limit:
        return result[-limit:]
    return result


def _local_models(config: TetherConfig) -> list[dict[str, Any]]:
    """Discover valid GGUF models without exposing unsupported snapshots."""
    try:
        from .cli import discover_models

        discovered = discover_models([Path(entry) for entry in config.gguf_dirs])
    except Exception:
        return []
    return [
        {
            "id": str(item.path.resolve()),
            "label": item.name,
            "description": f"{item.size_human} · {item.context_human} context",
            "context_size": item.context_length,
            "size_bytes": item.size_bytes,
        }
        for item in discovered
        if item.format == "gguf"
    ]


def provider_catalog(config: TetherConfig) -> list[dict[str, Any]]:
    """Return a secret-free catalog tailored to the current installation."""
    local_models = _local_models(config)
    providers: list[dict[str, Any]] = [{
        "id": "local",
        "label": "Local GGUF",
        "description": "Run a downloaded GGUF model through llama.cpp.",
        "requires_api_key": False,
        "api_key_configured": True,
        "models": local_models,
    }]
    for provider_id, preset in REMOTE_PROVIDERS.items():
        providers.append({
            "id": provider_id,
            "label": preset.get("label", provider_id.title()),
            "description": preset.get("description", ""),
            "requires_api_key": bool(preset.get("requires_api_key", True)),
            "api_key_configured": config.has_api_key(provider_id),
            "api_key_env": preset.get("api_key_env", ""),
            "models": copy.deepcopy(preset.get("models", [])),
        })
    providers.append({
        "id": "custom",
        "label": "Custom",
        "description": "Any OpenAI-compatible chat-completions endpoint.",
        "requires_api_key": True,
        "api_key_configured": config.has_api_key("custom"),
        "api_key_env": config.api_key_env if config.provider == "custom" else "",
        "models": [],
    })
    return providers


def bridge_status(
    config: TetherConfig,
    project: Path,
    *,
    todos: list[dict] | None = None,
    plan_mode: bool = False,
) -> dict[str, Any]:
    """Build the public, secret-free runtime status sent to the app."""
    provider = config.provider or "local"
    if provider == "local":
        model = str(Path(config.model_path).expanduser().resolve()) if config.model_path else ""
    else:
        model = config.api_model or "Default model"
    return {
        "type": "hello",
        "protocol": 3,
        "version": __version__,
        "project": str(project),
        "project_name": project.name,
        "provider": provider,
        "model": model,
        "context_size": config.context_size,
        "api_key_configured": config.has_api_key(provider),
        "reasoning_effort": config.reasoning_effort,
        "thinking_mode": config.thinking_mode,
        "api_base_url": config.api_base_url if provider == "custom" else "",
        "api_key_env": config.api_key_env,
        "providers": provider_catalog(config),
        "model_directories": list(config.gguf_dirs),
        "todos": todos or [],
        "memory_enabled": bool(getattr(config, "desktop_memory_enabled", False)),
        "plan_mode": plan_mode,
        "permissions": {
            "reads": "automatic" if config.auto_approve_reads else "ask",
            "edits": "automatic" if config.auto_approve_edits else "ask",
            "shell": "automatic" if config.auto_approve_bash else "ask",
        },
        "capabilities": {
            "approvals": True,
            "cancellation": True,
            "queued_followups": True,
            "agent_activity": True,
            "session_reset": True,
            "runtime_configuration": True,
            "session_configuration": True,
            "local_model_discovery": True,
            "clarifying_questions": True,
            "durable_todos": True,
            "background_jobs": True,
            "lsp_navigation": True,
            "workspace_write_sandbox": True,
            "slash_commands": True,
        },
    }


def _same_model_file(served: str, selected: str) -> bool:
    """True when a server's reported model id names the selected GGUF.

    llama-server reports the ``-m`` path; an alias (``--alias``) is compared
    by file name as a best effort.
    """
    try:
        if Path(served).expanduser().resolve() == Path(selected).expanduser().resolve():
            return True
    except OSError:
        pass
    return Path(served).name == Path(selected).name


class ProtocolWriter:
    """Thread-safe NDJSON writer that never follows redirected sys.stdout."""

    def __init__(self, stream: TextIO):
        self._stream = stream
        self._lock = threading.Lock()

    def send(self, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload, ensure_ascii=False, default=str)
        with self._lock:
            self._stream.write(encoded + "\n")
            self._stream.flush()


@dataclass
class _PendingApproval:
    event: threading.Event = field(default_factory=threading.Event)
    result: bool | str = False
    cancel_event: threading.Event | None = None


@dataclass(frozen=True)
class _QueuedTurn:
    turn_id: str
    prompt: str


@dataclass
class _PendingQuestions:
    event: threading.Event = field(default_factory=threading.Event)
    result: list[dict] = field(default_factory=list)


class AppBridgeServer:
    """One-project, one-conversation bridge process."""

    def __init__(
        self,
        project: Path,
        *,
        input_stream: TextIO | None = None,
        output_stream: TextIO | None = None,
    ):
        self.project = project
        self.input_stream = input_stream or sys.stdin
        if output_stream is None:
            # QueryEngine writes its terminal renderer to global sys.stdout.
            # Keep an independent descriptor so protocol messages cannot be
            # swallowed when a worker temporarily redirects that global.
            output_stream = os.fdopen(os.dup(sys.stdout.fileno()), "w", buffering=1)
        self.writer = ProtocolWriter(output_stream)
        self.config = TetherConfig.load()
        self.permissions = PermissionContext.from_config(self.config)
        self._busy = False
        self._state_lock = threading.Lock()
        self._cancel_event: threading.Event | None = None
        self._queued_turns: deque[_QueuedTurn] = deque()
        self._pending: dict[str, _PendingApproval] = {}
        self._pending_questions: dict[str, _PendingQuestions] = {}
        self._pending_lock = threading.Lock()
        self._shutdown = threading.Event()
        self._active_turn_id = ""
        self._last_stream_phase = ""
        self._local_server: subprocess.Popen | None = None
        project_key = hashlib.sha256(str(project).encode("utf-8")).hexdigest()[:20]
        self.todo_store = TodoStore(CONFIG_DIR / "app_state" / project_key / "todos.json")
        self.todo_tool: TodoWriteTool | None = None
        self.engine = self._build_engine()

    def _build_engine(self) -> QueryEngine:
        registry = ToolRegistry.build_default(
            include_codebase_model=getattr(self.config, "codebase_model_enabled", False),
            workspace_root=self.project,
            enforce_workspace=True,
        )
        # Match the terminal tool surface while keeping the bridge independent
        # of prompt_toolkit and terminal input handling.
        registry.tools["agent"] = AgentTool(
            self.config,
            registry,
            self.permissions,
            on_agent_update=self._forward_agent_update,
            on_approval_request=self._request_approval,
        )
        registry.tools["ask_user"] = AskUserTool(self._ask_clarifying_questions)
        self.todo_tool = TodoWriteTool(
            store=(
                self.todo_store
                if getattr(self.config, "desktop_memory_enabled", False)
                else None
            ),
            initial=[],
            on_update=self._on_todo_update,
        )
        registry.tools["todo_write"] = self.todo_tool
        return QueryEngine(
            config=self.config,
            tool_registry=registry,
            permissions=self.permissions,
            on_approval_request=self._request_approval,
            on_stream_event=self._forward_stream_event,
            persistent_context_enabled=getattr(
                self.config, "desktop_memory_enabled", False
            ),
            # The terminal's last-session summary is global rather than
            # workspace-scoped. Desktop memory deliberately excludes it so a
            # summary from one project cannot seed paths into another.
            include_last_session_summary=False,
        )

    def _forward_stream_event(self, event: StreamEvent) -> None:
        turn_id = self._active_turn_id
        if not turn_id:
            return
        if event.type == "thinking" and self._last_stream_phase == "thinking":
            return
        self._last_stream_phase = event.type
        self.writer.send({
            "type": "turn_delta",
            "id": turn_id,
            "kind": event.type,
            "text": event.text,
            "tool": event.tool_name,
            "is_error": event.is_error,
            "tool_call_id": event.tool_call_id,
            "tool_args_preview": event.tool_args_preview,
            "tool_output_preview": event.tool_output_preview,
            "display_kind": event.display_kind,
            "error_code": event.error_code,
            "metadata": event.metadata,
        })

    def _forward_agent_update(self, update: dict[str, Any]) -> None:
        """Attach one safe sub-agent snapshot to its active parent turn."""
        with self._state_lock:
            turn_id = self._active_turn_id
        if not turn_id:
            return
        payload = dict(update)
        payload["type"] = "agent_updated"
        payload["id"] = turn_id
        self.writer.send(payload)

    def _on_todo_update(self, todos: list[dict]) -> None:
        self.writer.send({"type": "todo_updated", "todos": todos})

    def _command_catalog(self) -> list[dict[str, str]]:
        catalog = [
            {"command": command, "description": description, "category": category}
            for command, description, category in _APP_COMMAND_SPECS
        ]
        reserved = {entry[0].split(maxsplit=1)[0].lstrip("/") for entry in _APP_COMMAND_SPECS}
        seen = set(reserved)
        try:
            from .core.skills import default_store

            store = default_store()
            toolsets = set(self.engine.tools.names())
            for bundle in sorted(store.bundles(), key=lambda item: item.slug):
                if bundle.slug in reserved:
                    continue
                catalog.append({
                    "command": f"/{bundle.slug}",
                    "description": bundle.description or f"Load the {bundle.name} skill bundle",
                    "category": "bundle",
                })
                seen.add(bundle.slug)
            for skill in sorted(store.available(toolsets), key=lambda item: item.slug):
                if skill.slug in seen:
                    continue
                hint = f" · args: {skill.argument_hint}" if skill.argument_hint else ""
                catalog.append({
                    "command": f"/{skill.slug}",
                    "description": (skill.description or f"Apply the {skill.name} skill") + hint,
                    "category": "skill",
                })
                seen.add(skill.slug)
        except Exception:
            pass
        return catalog

    def _status(self) -> dict[str, Any]:
        todos = self.todo_tool.snapshot() if self.todo_tool is not None else self.todo_store.load()
        status = bridge_status(
            self.config,
            self.project,
            todos=todos,
            plan_mode=self.engine.plan_mode,
        )
        status["tools"] = [
            {"name": tool.name, "description": tool.description}
            for tool in self.engine.tools.definitions()
        ]
        status["commands"] = self._command_catalog()
        return status

    def _ask_clarifying_questions(self, questions: list[dict]) -> list[dict]:
        request_id = uuid.uuid4().hex
        pending = _PendingQuestions()
        with self._pending_lock:
            self._pending_questions[request_id] = pending
        self.writer.send({
            "type": "questions_required",
            "request_id": request_id,
            "questions": questions,
        })
        while not pending.event.wait(0.1):
            cancel = self._cancel_event
            if self._shutdown.is_set() or (cancel is not None and cancel.is_set()):
                break
        with self._pending_lock:
            self._pending_questions.pop(request_id, None)
        return pending.result

    def _resolve_questions(self, message: dict[str, Any]) -> None:
        request_id = str(message.get("request_id", ""))
        with self._pending_lock:
            pending = self._pending_questions.get(request_id)
        if pending is None:
            self.writer.send({
                "type": "error",
                "message": "Those questions are no longer active.",
            })
            return
        raw_answers = message.get("answers")
        if isinstance(raw_answers, list):
            pending.result = [item for item in raw_answers if isinstance(item, dict)]
        pending.event.set()

    def _request_approval(self, tool_name: str, arguments: dict) -> bool | str:
        request_id = uuid.uuid4().hex
        with self._state_lock:
            cancel_event = self._cancel_event
        pending = _PendingApproval(cancel_event=cancel_event)
        with self._pending_lock:
            self._pending[request_id] = pending
        self.writer.send({
            "type": "approval_required",
            "request_id": request_id,
            "tool": tool_name,
            "arguments": arguments,
        })

        while not pending.event.wait(0.1):
            if self._shutdown.is_set() or (
                pending.cancel_event is not None and pending.cancel_event.is_set()
            ):
                with self._pending_lock:
                    cancelled = self._pending.pop(request_id, None) is pending
                if cancelled:
                    pending.result = False
                    break
                # A resolver already consumed this request and may have set
                # the cancel token to stop sibling work for an explicit No.
                # Let that resolver publish its distinct decision instead of
                # overwriting it with the ordinary-Stop value.
                continue
        return pending.result

    def _resolve_approval(self, message: dict[str, Any]) -> None:
        request_id = str(message.get("request_id", ""))
        with self._pending_lock:
            # Consume the request before mutating it. A double click, delayed
            # WebView event, or Stop/Allow race can therefore resolve it only
            # once and can never overwrite a decision already being handled.
            pending = self._pending.pop(request_id, None)
        if pending is None:
            self.writer.send({
                "type": "approval_resolved",
                "request_id": request_id,
                "decision": str(message.get("decision", "deny")),
                "accepted": False,
                "stale": True,
            })
            return

        decision = str(message.get("decision", "deny"))
        if decision == "allow_once":
            pending.result = True
        elif decision == "allow_session":
            pending.result = "ALL"
        elif decision == "feedback":
            pending.result = str(message.get("feedback", "")).strip() or False
        else:
            decision = "deny"
            pending.result = APPROVAL_DENIED_SIGNAL
        if decision == "deny":
            # Discard follow-ups that were written for the now-aborted course
            # before the engine can wake and advance to the next queued turn.
            self._clear_queued_turns("approval_denied")
            if pending.cancel_event is not None:
                pending.cancel_event.set()
        pending.event.set()
        self.writer.send({
            "type": "approval_resolved",
            "request_id": request_id,
            "decision": decision,
            "accepted": True,
            "stale": False,
        })

    def _release_pending(self) -> None:
        with self._pending_lock:
            pending = list(self._pending.values())
            questions = list(self._pending_questions.values())
            self._pending.clear()
            self._pending_questions.clear()
        for item in pending:
            item.result = False
            item.event.set()
        for item in questions:
            item.result = []
            item.event.set()

    def _clear_queued_turns(self, reason: str) -> list[str]:
        with self._state_lock:
            turn_ids = [item.turn_id for item in self._queued_turns]
            self._queued_turns.clear()
        if turn_ids:
            self.writer.send({
                "type": "turn_queue_cleared",
                "ids": turn_ids,
                "reason": reason,
            })
        return turn_ids

    def _start_submit(self, message: dict[str, Any]) -> None:
        prompt = str(message.get("prompt", "")).strip()
        turn_id = str(message.get("id", "")) or uuid.uuid4().hex
        if not prompt:
            self.writer.send({"type": "error", "message": "The prompt is empty.", "id": turn_id})
            return
        with self._state_lock:
            if self._busy:
                if not self._active_turn_id:
                    self.writer.send({
                        "type": "error",
                        "message": "Wait for the active command to finish before sending a prompt.",
                        "id": turn_id,
                    })
                    return
                self._queued_turns.append(_QueuedTurn(turn_id=turn_id, prompt=prompt))
                position = len(self._queued_turns)
                self.writer.send({
                    "type": "turn_queued",
                    "id": turn_id,
                    "position": position,
                })
                return
            else:
                self._busy = True
                self._cancel_event = threading.Event()
                cancel_event = self._cancel_event
                self._active_turn_id = turn_id
                self._last_stream_phase = ""

        worker = threading.Thread(
            target=self._submit_worker,
            args=(turn_id, prompt, cancel_event),
            daemon=True,
            name="tether-app-turn",
        )
        worker.start()

    def _submit_worker(
        self, turn_id: str, prompt: str, cancel_event: threading.Event
    ) -> None:
        mark_interactive(True)
        try:
            while True:
                self.writer.send({"type": "turn_started", "id": turn_id})
                before_input = self.engine.usage.input_tokens
                before_output = self.engine.usage.output_tokens
                captured = io.StringIO()
                stop_reason = "failed"
                try:
                    with contextlib.redirect_stdout(captured):
                        result = self.engine.submit(prompt, cancel_event=cancel_event)
                    stop_reason = result.stop_reason
                    self.writer.send({
                        "type": "turn_completed",
                        "id": turn_id,
                        "output": result.output,
                        "stop_reason": result.stop_reason,
                        "tool_calls": result.tool_calls_made,
                        "usage": {
                            "input_tokens": result.usage.input_tokens - before_input,
                            "output_tokens": result.usage.output_tokens - before_output,
                            "total_tokens": (
                                result.usage.input_tokens - before_input
                                + result.usage.output_tokens - before_output
                            ),
                        },
                        "diagnostic_output": clean_terminal_output(captured.getvalue()),
                    })
                except Exception as exc:
                    self.writer.send({
                        "type": "turn_failed",
                        "id": turn_id,
                        "message": str(exc),
                        "diagnostic_output": clean_terminal_output(captured.getvalue()),
                    })

                self._release_pending()
                with self._state_lock:
                    if self._queued_turns:
                        queued_turn = self._queued_turns.popleft()
                        turn_id = queued_turn.turn_id
                        prompt = queued_turn.prompt
                        cancel_event = threading.Event()
                        self._cancel_event = cancel_event
                        self._active_turn_id = turn_id
                        self._last_stream_phase = ""
                        idle = False
                    else:
                        self._busy = False
                        self._cancel_event = None
                        self._active_turn_id = ""
                        idle = True
                        if stop_reason == "approval_denied":
                            self.writer.send({
                                "type": "direction_required",
                                "id": turn_id,
                                "message": "The command was not run. What should Tether do instead?",
                            })
                        # Emit while holding the state lock. A prompt arriving
                        # at this boundary cannot start a new worker before the
                        # authoritative idle event reaches the WebView.
                        self.writer.send({"type": "bridge_idle"})
                if idle:
                    break
        finally:
            mark_interactive(False)
            orphaned: list[str] = []
            with self._state_lock:
                if self._busy and self._active_turn_id == turn_id:
                    orphaned = [item.turn_id for item in self._queued_turns]
                    self._queued_turns.clear()
                    self._busy = False
                    self._cancel_event = None
                    self._active_turn_id = ""
                    self.writer.send({"type": "bridge_idle"})
            if orphaned:
                self.writer.send({
                    "type": "turn_queue_cleared",
                    "ids": orphaned,
                    "reason": "turn_worker_failed",
                })

    def _stop_local_server(self) -> None:
        process = self._local_server
        self._local_server = None
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()

    def _configure_runtime(self, message: dict[str, Any]) -> None:
        with self._state_lock:
            if self._busy:
                self.writer.send({
                    "type": "error",
                    "message": "Wait for the active turn to finish before changing models.",
                })
                return

        provider = str(message.get("provider", "")).strip().lower()
        model_id = str(message.get("model", "")).strip()
        previous = copy.deepcopy(self.config.__dict__)
        try:
            if provider == "local":
                known = {item["id"]: item for item in _local_models(self.config)}
                if model_id not in known:
                    raise ValueError("Select a discovered local GGUF model first.")
                self.config.model_path = model_id
                context_size = known[model_id].get("context_size")
                if isinstance(context_size, int) and context_size > 0:
                    self.config.local_context_size = min(context_size, 131_072)
                apply_provider_selection(self.config, "local")
            else:
                apply_provider_selection(
                    self.config,
                    provider,
                    model_id,
                    reasoning_effort=(
                        str(message["reasoning_effort"])
                        if "reasoning_effort" in message
                        else None
                    ),
                    thinking_mode=(
                        str(message["thinking_mode"])
                        if "thinking_mode" in message
                        else None
                    ),
                    base_url=str(message.get("api_base_url", "")),
                    api_key_env=str(message.get("api_key_env", "")),
                )

            api_key = normalize_api_key(str(message.get("api_key", "")))
            if api_key:
                self.config.api_keys[provider] = api_key
            if message.get("clear_api_key"):
                self.config.api_keys.pop(provider, None)
                if self.config.provider == provider:
                    self.config.api_key = ""
            self.config.save()

            if provider == "local":
                self._stop_local_server()
                from .cli import (
                    _launch_server_background,
                    served_model_path,
                    server_is_running,
                    stop_server_on_port,
                )

                # _launch_server_background treats any healthy listener on the
                # port as "ready", so a server started outside this app (a
                # `tether serve`, or one orphaned by a previous bridge) would
                # silently keep serving the *old* model while the new one died
                # on bind. Reuse it when it already serves the selected model;
                # otherwise replace it.
                launch = True
                if server_is_running(self.config):
                    served = served_model_path(self.config)
                    if served and _same_model_file(served, self.config.model_path):
                        launch = False  # already serving this model; adopt it
                    elif not stop_server_on_port(self.config):
                        raise RuntimeError(
                            f"A llama-server on port {self.config.port} is serving a "
                            "different model and could not be stopped. Run `tether stop` "
                            "and try again."
                        )

                if launch:
                    captured = io.StringIO()
                    with contextlib.redirect_stdout(captured):
                        self._local_server = _launch_server_background(
                            self.config, Path(self.config.model_path)
                        )
                    if self._local_server is None:
                        diagnostic = clean_terminal_output(captured.getvalue(), limit=800)
                        raise RuntimeError(diagnostic or "Could not start the local llama.cpp server.")
            else:
                self._stop_local_server()
        except Exception as exc:
            self.config.__dict__.clear()
            self.config.__dict__.update(previous)
            try:
                self.config.save()
            except OSError:
                pass
            self.writer.send({"type": "runtime_config_failed", "message": str(exc)})
            return

        status = self._status()
        status["type"] = "runtime_configured"
        self.writer.send(status)

    def _configure_session(self, message: dict[str, Any]) -> None:
        """Apply desktop-only continuity and planning settings safely."""
        with self._state_lock:
            if self._busy:
                self.writer.send({
                    "type": "error",
                    "message": "Wait for the active turn to finish before changing session settings.",
                })
                return

        has_memory = "memory_enabled" in message
        has_plan = "plan_mode" in message
        if not has_memory and not has_plan:
            self.writer.send({
                "type": "error",
                "message": "Choose a memory or plan-mode setting to change.",
            })
            return
        if has_memory and not isinstance(message["memory_enabled"], bool):
            self.writer.send({"type": "error", "message": "memory_enabled must be a boolean."})
            return
        if has_plan and not isinstance(message["plan_mode"], bool):
            self.writer.send({"type": "error", "message": "plan_mode must be a boolean."})
            return

        previous_memory = bool(getattr(self.config, "desktop_memory_enabled", False))
        try:
            if has_memory:
                enabled = message["memory_enabled"]
                self.config.desktop_memory_enabled = enabled
                self.config.save()
                self.engine.set_persistent_context_enabled(enabled)
                if self.todo_tool is not None:
                    # Checklists are part of desktop continuity. Keep the live
                    # checklist in memory, but only load/save it across launches
                    # while Memory is enabled.
                    self.todo_tool.store = self.todo_store if enabled else None
                    if enabled:
                        self.todo_store.save(self.todo_tool.snapshot())
            if has_plan:
                self.engine.plan_mode = message["plan_mode"]
        except (OSError, ValueError) as exc:
            self.config.desktop_memory_enabled = previous_memory
            try:
                self.config.save()
            except OSError:
                pass
            self.engine.set_persistent_context_enabled(previous_memory)
            if self.todo_tool is not None:
                self.todo_tool.store = self.todo_store if previous_memory else None
            self.writer.send({"type": "error", "message": f"Could not update session settings: {exc}"})
            return

        status = self._status()
        status["type"] = "session_configured"
        self.writer.send(status)

    def _add_model_directory(self, message: dict[str, Any]) -> None:
        directory = Path(str(message.get("path", ""))).expanduser().resolve()
        if not directory.is_dir():
            self.writer.send({"type": "error", "message": f"Model directory not found: {directory}"})
            return
        value = str(directory)
        if value not in self.config.gguf_dirs:
            self.config.gguf_dirs.append(value)
            self.config.save()
        status = self._status()
        status["type"] = "catalog_updated"
        self.writer.send(status)

    def _reset(self) -> None:
        with self._state_lock:
            if self._busy:
                self.writer.send({
                    "type": "error",
                    "message": "Cancel the active turn before starting a new session.",
                })
                return
            self.permissions = PermissionContext.from_config(self.config)
            previous_engine = self.engine
            if self.todo_tool is not None:
                self.todo_tool.clear()
            previous_engine.tools.close()
            self.engine = self._build_engine()
        self.writer.send({
            "type": "session_reset",
            "todos": [],
            "memory_enabled": bool(
                getattr(self.config, "desktop_memory_enabled", False)
            ),
            "plan_mode": self.engine.plan_mode,
        })

    def _send_command_result(
        self,
        request_id: str,
        raw: str,
        message: str,
        *,
        ok: bool = True,
    ) -> None:
        self.writer.send({
            "type": "command_result",
            "id": request_id,
            "command": raw,
            "ok": ok,
            "message": message,
            "plan_mode": self.engine.plan_mode,
        })

    def _skill_command_prompt(self, raw: str) -> str | None:
        head, _, remainder = raw[1:].partition(" ")
        try:
            from .core.skills import default_store

            store = default_store()
            resolved = store.resolve_command(head)
            if resolved is None:
                return None
            kind, item = resolved
            arguments = remainder.strip()
            task = arguments or "Apply this skill to the current project context."

            def render(body: str) -> str:
                words = arguments.split()
                rendered = body.replace("$ARGUMENTS", arguments)
                rendered = re.sub(
                    r"\$([1-9])",
                    lambda match: (
                        words[int(match.group(1)) - 1]
                        if int(match.group(1)) <= len(words)
                        else ""
                    ),
                    rendered,
                )
                return rendered[:8_000]

            if kind == "bundle":
                members = store.bundle_members(item)
                if not members:
                    return None
                sections = "\n\n".join(
                    f"## Skill: {skill.name}\n{render(skill.body)}" for skill in members
                )
                return (
                    f"[BUNDLE INVOKED: {item.name}]\n"
                    f"Apply these procedures together and reconcile any overlap.\n\n"
                    f"{sections}\n\n--- Task ---\n{task}"
                )
            return (
                f"[SKILL INVOKED: {item.name}]\n"
                f"Follow this procedure for the task below.\n\n"
                f"{render(item.body)}\n\n--- Task ---\n{task}"
            )
        except Exception:
            return None

    def _workflow_command_prompt(self, command: str, argument: str) -> str | None:
        target = argument.strip() or "the current project and active worktree"
        prompts = {
            "/why": (
                f"Explain why {target} exists or changed. Inspect the referenced code and surrounding "
                "callers, then give an evidence-backed rationale with exact paths and lines."
            ),
            "/selfcheck": (
                "Run a Tether installation self-check. Verify Python compilation and imports first"
                + (", then run the full unit suite" if argument.strip().lower() == "deep" else "")
                + ". Report each command, its result, and any actionable failures."
            ),
        }
        return prompts.get(command)

    def _persistence_command(
        self,
        argument: str,
        on_progress=None,
    ) -> tuple[bool, str]:
        if not getattr(self.config, "codebase_model_enabled", False):
            return False, "The persistent codebase mental model is disabled in Tether configuration."
        pieces = argument.split()
        action = pieces[0].lower() if pieces else "status"
        try:
            if action == "gc":
                from .core.codebase_model.service import gc_models

                report = gc_models()
                removed = report.get("removed", [])
                return True, (
                    f"Mental-model cleanup removed **{len(removed)}** orphaned database(s) and kept "
                    f"**{len(report.get('kept', []))}** live database(s)."
                )

            from .core.codebase_model.service import get_model

            model = get_model(self.project)
            if action == "build":
                report = model.build(on_progress=on_progress)
                return True, (
                    f"Mental model built: **{report.get('files', 0)} files**, "
                    f"**{report.get('indexed', 0)} indexed**, "
                    f"**{model.store.count_nodes()} symbols**."
                )
            if action == "sync":
                report = model.sync()
                return True, (
                    f"Mental model synchronized: **{report.get('indexed', 0)} changed**, "
                    f"**{report.get('removed', 0)} removed**, "
                    f"**{report.get('invalidated', 0)} beliefs invalidated**."
                )
            if action == "check":
                files = pieces[1:] or None
                violations = (
                    model.invariants.check_diff(files)
                    if files
                    else model.invariants.check_all()
                )
                violations += model.invariants.detect_rejected(files or model.store.all_files())
                if not violations:
                    return True, "Mental-model check passed with **no invariant violations**."
                rows = [
                    f"- **{'BLOCKING' if item.blocking else 'soft'}**: {item.claim} at `{item.location}`"
                    for item in violations[:50]
                ]
                return False, "Mental-model violations:\n\n" + "\n".join(rows)
            if action == "ask":
                question = argument.split(maxsplit=1)[1].strip() if " " in argument else ""
                if not question:
                    return False, "Add a question after `/persistence ask`."
                return True, model.answer(question)
            if action not in {"", "status"}:
                return False, "Use status, build, sync, check, ask, or gc with `/persistence`."

            store = model.store
            commit = store.get_meta("commit") or "unbuilt"
            return True, (
                f"Mental model at `{model.repo_root}`\n\n"
                f"- Commit: `{commit}`\n"
                f"- **{store.count_nodes()}** symbols, **{len(store.all_edges())}** edges, "
                f"**{len(store.all_files())}** files\n"
                f"- **{store.count_beliefs()}** beliefs, **{len(store.all_invariants())}** invariants, "
                f"**{len(store.all_decisions())}** decisions"
            )
        except Exception as exc:
            return False, f"Mental model command failed: {exc}"

    def _start_persistence_command(self, request_id: str, raw: str, argument: str) -> None:
        with self._state_lock:
            if self._busy:
                self._send_command_result(
                    request_id,
                    raw,
                    "Wait for the active turn to finish or stop it before running a command.",
                    ok=False,
                )
                return
            self._busy = True
            self._cancel_event = threading.Event()

        worker = threading.Thread(
            target=self._persistence_worker,
            args=(request_id, raw, argument),
            daemon=True,
            name="tether-persistence-command",
        )
        worker.start()

    def _persistence_worker(self, request_id: str, raw: str, argument: str) -> None:
        action = argument.split(maxsplit=1)[0].lower() if argument.strip() else "status"
        labels = {
            "status": "Reading the codebase mental model…",
            "build": "Indexing the repository mental model…",
            "sync": "Synchronizing changed files…",
            "check": "Checking recorded invariants…",
            "ask": "Querying the codebase mental model…",
            "gc": "Cleaning up orphaned model databases…",
        }
        # A status read normally completes in milliseconds. Avoid an extra UI
        # state/render cycle for that fast path; reserve progress events for
        # operations that can do material work.
        if action != "status":
            self.writer.send({
                "type": "command_progress",
                "id": request_id,
                "command": raw,
                "message": labels.get(action, "Running the mental-model command…"),
            })

        last_percent = -5

        def report_progress(done: int, total: int, path: str) -> None:
            nonlocal last_percent
            percent = int((done / total) * 100) if total else 100
            if percent < 100 and percent < last_percent + 5:
                return
            last_percent = percent
            self.writer.send({
                "type": "command_progress",
                "id": request_id,
                "command": raw,
                "message": f"Indexing mental model… {percent}% ({done}/{total})",
                "path": path,
            })

        try:
            ok, result = self._persistence_command(argument, on_progress=report_progress)
        except Exception as exc:
            ok, result = False, f"Mental model command failed: {exc}"
        with self._state_lock:
            self._busy = False
            self._cancel_event = None
        self._send_command_result(request_id, raw, result, ok=ok)

    def _run_command(self, message: dict[str, Any]) -> None:
        raw = str(message.get("command", "")).strip()
        request_id = str(message.get("id", "")) or uuid.uuid4().hex
        parts = raw.split(maxsplit=1)
        command = parts[0].lower() if parts else ""
        argument = parts[1].strip() if len(parts) > 1 else ""
        normalized_argument = argument.lower()

        with self._state_lock:
            busy = self._busy
        if busy:
            self._send_command_result(
                request_id,
                raw,
                "Wait for the active turn to finish or stop it before running a command.",
                ok=False,
            )
            return

        if command == "/plan":
            if normalized_argument in {"on", "enable"}:
                self.engine.plan_mode = True
            elif normalized_argument in {"off", "disable"}:
                self.engine.plan_mode = False
            elif normalized_argument:
                self._send_command_result(
                    request_id, raw, "Use `/plan`, `/plan on`, or `/plan off`.", ok=False
                )
                return
            else:
                self.engine.plan_mode = not self.engine.plan_mode
            enabled = self.engine.plan_mode
            result = (
                "Plan mode is **on**. Tether will investigate first and plan before changing files."
                if enabled
                else "Plan mode is **off**. Tether will use its normal execution mode."
            )
        elif command == "/usage":
            result = (
                f"Session usage: **{self.engine.usage.input_tokens:,} input**, "
                f"**{self.engine.usage.output_tokens:,} output**, "
                f"**{self.engine.usage.total:,} total tokens**."
            )
        elif command == "/context":
            percent = max(0.0, self.engine.get_context_usage_ratio() * 100)
            result = (
                f"Estimated context usage is **{percent:.1f}%** across "
                f"**{len(self.engine.messages)} messages**."
            )
        elif command == "/session":
            result = (
                f"Project: `{self.project}`\n\n"
                f"Runtime: **{self.config.provider} / {self.config.api_model}**\n\n"
                f"Plan mode: **{'on' if self.engine.plan_mode else 'off'}**"
            )
        elif command == "/model":
            result = (
                f"Current model: **{self.config.api_model}** on **{self.config.provider}**. "
                f"Context size: **{self.config.context_size:,}** tokens."
            )
        elif command == "/provider":
            result = f"Current provider: **{self.config.provider}**. Use Runtime settings to change it."
        elif command == "/persistence":
            self._start_persistence_command(request_id, raw, argument)
            return
        elif command == "/compact":
            before = len(self.engine.messages)
            self.engine._compact()
            after = len(self.engine.messages)
            result = f"Context compacted from **{before}** to **{after} messages**."
        elif command == "/history":
            recent = self.engine.messages[-12:]
            if not recent:
                result = "No conversation history yet."
            else:
                rows = []
                for item in recent:
                    preview = " ".join((item.content or "").split())[:120]
                    rows.append(f"- **{item.role}**: {preview or '(tool call)'}")
                result = "Recent model history:\n\n" + "\n".join(rows)
        elif command == "/skills":
            from .core.skills import default_store

            store = default_store()
            subparts = argument.split()
            action = subparts[0].lower() if subparts else "list"
            if action == "doctor":
                problems = store.diagnostics()
                result = (
                    "All skill and bundle files parse cleanly."
                    if not problems
                    else "Skill diagnostics:\n\n" + "\n".join(f"- {item}" for item in problems)
                )
            elif action == "show" and len(subparts) > 1:
                resolved = store.resolve_command(subparts[1])
                if resolved is None:
                    self._send_command_result(
                        request_id, raw, f"No skill or bundle named `{subparts[1]}`.", ok=False
                    )
                    return
                kind, item = resolved
                result = (
                    f"**{item.name}** bundle\n\n{item.description}\n\nLoads: "
                    + ", ".join(item.skills)
                    if kind == "bundle"
                    else f"# {item.name}\n\n{item.description}\n\n{item.body}"
                )
            else:
                skills = store.available(set(self.engine.tools.names()))
                bundles = store.bundles()
                rows = [f"- `/{item.slug}` — {item.description}" for item in bundles]
                rows += [f"- `/{item.slug}` — {item.description}" for item in skills]
                result = "Available skills and bundles:\n\n" + "\n".join(rows)
        elif command == "/learn":
            if normalized_argument.startswith("auto"):
                mode = normalized_argument.split(maxsplit=1)[1] if " " in normalized_argument else ""
                if mode in {"on", "enable"}:
                    self.config.self_learning = True
                elif mode in {"off", "disable"}:
                    self.config.self_learning = False
                else:
                    result = f"Automatic workflow learning is **{'on' if self.config.self_learning else 'off'}**."
                    self._send_command_result(request_id, raw, result)
                    return
                self.config.save()
                result = f"Automatic workflow learning is now **{'on' if self.config.self_learning else 'off'}**."
            else:
                source = argument or "this conversation"
                prompt = (
                    f"Author a reusable Tether skill from {source!r}. Identify the repeatable workflow, "
                    "then use skill_manage to create a skill with When to Use, Procedure, Pitfalls, and "
                    "Verification sections. Confirm the saved skill name."
                )
                self._start_submit({"id": request_id, "prompt": prompt})
                return
        elif command == "/summaries":
            from .core.config import CONFIG_DIR

            directory = CONFIG_DIR / "summaries"
            files = sorted(directory.glob("*.txt"), key=lambda path: path.stat().st_mtime, reverse=True) if directory.exists() else []
            result = (
                "No saved conversation summaries."
                if not files
                else "Recent summaries:\n\n" + "\n".join(f"- `{path.stem}`" for path in files[:10])
            )
        elif command == "/memory":
            from .engine.query_engine import MEMORY_FILE, MEMORY_UPDATE_PROMPT, load_memory, save_memory
            from .core.models import Message

            action = normalized_argument or "update"
            if action == "show":
                memory = load_memory()
                result = f"# Persistent memory\n\n{memory}" if memory else "No persistent memory yet."
            elif action == "clear":
                if MEMORY_FILE.exists():
                    MEMORY_FILE.unlink()
                    result = "Persistent memory cleared."
                else:
                    result = "No persistent memory to clear."
            elif action == "update":
                if len(self.engine.messages) <= 2:
                    result = "There is not enough conversation to update persistent memory yet."
                else:
                    current = load_memory() or "(empty — first update)"
                    prompt = MEMORY_UPDATE_PROMPT.format(current_memory=current)
                    response, _ = self.engine.backend.chat_completion(
                        messages=self.engine.messages + [Message(role="user", content=prompt)],
                        tools=None,
                        temperature=0.3,
                        max_tokens=2_048,
                    )
                    updated = (response.content or "").strip()
                    if updated:
                        path = save_memory(updated)
                        result = f"Persistent memory updated at `{path}`."
                    else:
                        result = "The model returned no memory update."
            else:
                self._send_command_result(
                    request_id, raw, "Use `/memory`, `/memory show`, or `/memory clear`.", ok=False
                )
                return
        elif command in {"/save", "/resume", "/also", "/redirect", "/stop"}:
            result = {
                "/save": "Desktop session-file saving is not wired yet; the active conversation remains available while this window is open.",
                "/resume": "Desktop session restore is not wired yet. Terminal sessions remain available through `tether start`.",
                "/also": "Add a prompt after `/also`; desktop follow-ups are queued while Tether is working.",
                "/redirect": "Use Stop, then send the replacement direction.",
                "/stop": "There is no active turn. Use the purple Stop button while Tether is running.",
            }[command]
        elif command in {"/help", "/", "/?"}:
            result = (
                "Type `/` or use the command button to browse conversation, mental-model, "
                "skills, memory, session, verification, runtime, and control commands."
            )
        else:
            workflow_prompt = self._workflow_command_prompt(command, argument)
            skill_prompt = self._skill_command_prompt(raw) if workflow_prompt is None else None
            prompt = workflow_prompt or skill_prompt
            if prompt is not None:
                self._start_submit({"id": request_id, "prompt": prompt})
                return
            self._send_command_result(
                request_id,
                raw,
                f"Unknown command: `{raw or '(empty)'}`. Type `/` to see supported commands.",
                ok=False,
            )
            return

        self._send_command_result(request_id, raw, result)

    def handle(self, message: dict[str, Any]) -> bool:
        kind = str(message.get("type", ""))
        if kind == "submit":
            self._start_submit(message)
        elif kind == "command":
            self._run_command(message)
        elif kind == "cancel":
            with self._state_lock:
                cancel = self._cancel_event
                queued_ids = [item.turn_id for item in self._queued_turns]
                self._queued_turns.clear()
            if cancel is not None:
                cancel.set()
            self._release_pending()
            if queued_ids:
                self.writer.send({
                    "type": "turn_queue_cleared",
                    "ids": queued_ids,
                    "reason": "cancelled",
                })
            self.writer.send({"type": "cancel_requested"})
        elif kind == "approval_response":
            self._resolve_approval(message)
        elif kind == "questions_response":
            self._resolve_questions(message)
        elif kind == "reset":
            self._reset()
        elif kind == "status":
            self.writer.send(self._status())
        elif kind == "configure_runtime":
            self._configure_runtime(message)
        elif kind == "configure_session":
            self._configure_session(message)
        elif kind == "add_model_directory":
            self._add_model_directory(message)
        elif kind == "ping":
            self.writer.send({"type": "pong"})
        elif kind == "shutdown":
            with self._state_lock:
                cancel = self._cancel_event
                self._queued_turns.clear()
            if cancel is not None:
                cancel.set()
            self._shutdown.set()
            self._stop_local_server()
            self._release_pending()
            self.engine.tools.close()
            self.writer.send({"type": "goodbye"})
            return False
        else:
            self.writer.send({"type": "error", "message": f"Unknown message type: {kind or '(empty)'}"})
        return True

    def serve(self) -> int:
        self.writer.send(self._status())
        for raw_line in self.input_stream:
            if self._shutdown.is_set():
                break
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                message = json.loads(raw_line)
                if not isinstance(message, dict):
                    raise ValueError("message must be a JSON object")
            except (json.JSONDecodeError, ValueError) as exc:
                self.writer.send({"type": "error", "message": f"Invalid bridge message: {exc}"})
                continue
            if not self.handle(message):
                break
        self._shutdown.set()
        with self._state_lock:
            self._queued_turns.clear()
        self._release_pending()
        self._stop_local_server()
        self.engine.tools.close()
        return 0


def run_app_bridge(args: argparse.Namespace) -> int:
    project = Path(args.project).expanduser().resolve()
    if not project.is_dir():
        print(json.dumps({"type": "fatal", "message": f"Project directory not found: {project}"}))
        return 2
    os.chdir(project)
    return AppBridgeServer(project).serve()
