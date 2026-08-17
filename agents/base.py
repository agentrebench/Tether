"""Sub-agent framework for Tether.

Each agent gets its own context window, system prompt, and tool access.
Agents are spawned by the main engine when the model calls the 'agent' tool.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Callable

from ..core.config import TetherConfig
from ..core.models import Message, UsageSummary
from ..core.permissions import APPROVAL_DENIED_SIGNAL, PermissionContext
from ..engine.backend import InferenceBackend, MALFORMED_ARGS_KEY
from ..tools.base import ToolRegistry


@dataclass(frozen=True)
class AgentDefinition:
    name: str
    description: str
    system_prompt: str
    allowed_tools: list[str] = field(default_factory=list)  # empty = all tools
    max_turns: int = 20


@dataclass
class AgentResult:
    agent_name: str
    output: str
    task: str = ""
    agent_index: int = 1  # slot within a fan-out batch (1..N), for result ordering
    display_label: str = ""  # human label shown in the UI, e.g. "agent 3"
    tool_calls_made: list[str] = field(default_factory=list)
    usage: UsageSummary = field(default_factory=UsageSummary)
    status: str = "completed"
    error_code: str = ""
    error: str | None = None


@dataclass(frozen=True)
class AgentToolEvent:
    """Safe lifecycle signal for a tool requested inside a sub-agent.

    Raw tool output and model text are deliberately excluded. Consumers can
    derive a whitelisted argument preview without ever receiving reasoning or
    an exception traceback.
    """

    tool_call_id: str
    tool_name: str
    status: str
    arguments: dict = field(default_factory=dict)
    is_error: bool = False
    error_code: str = ""


class SubAgent:
    """A sub-agent that runs in its own message context."""

    def __init__(
        self,
        definition: AgentDefinition,
        config: TetherConfig,
        tool_registry: ToolRegistry,
        permissions: PermissionContext,
        agent_index: int = 1,
        display_label: str | None = None,
        on_tool_call: callable = None,
        on_tool_event: Callable[[AgentToolEvent], None] | None = None,
        on_status: callable = None,
        on_approval_request: Callable[[str, dict], bool | str] | None = None,
        cancel_event=None,  # threading.Event | None — user interrupt
    ):
        self.definition = definition
        self.config = config
        self.backend = InferenceBackend(config)
        self.permissions = permissions
        self.cancel_event = cancel_event
        self.agent_index = agent_index
        # `agent_index` is only a batch slot; the label is what the user sees and
        # must be unique across every agent launched, including agents started as
        # separate top-level tool calls (which all share agent_index == 1).
        self.display_label = display_label or f"agent {agent_index}"
        self.on_tool_call = on_tool_call
        self.on_tool_event = on_tool_event
        self.on_status = on_status
        self.on_approval_request = on_approval_request

        # Filter tools if agent has restricted access
        if definition.allowed_tools:
            filtered = {
                name: tool
                for name, tool in tool_registry.tools.items()
                if name in definition.allowed_tools
            }
            self.tools = ToolRegistry(tools=filtered)
        else:
            self.tools = tool_registry

        self.messages: list[Message] = [
            Message(role="system", content=definition.system_prompt)
        ]
        self.usage = UsageSummary()
        self._recent_calls: list[tuple[str, str]] = []
        self._recent_errors: list[tuple[str, str]] = []

    def run(self, task: str) -> AgentResult:
        self.messages.append(Message(role="user", content=task))
        all_tool_calls = []
        final_text = ""
        final_status = "completed"
        final_error_code = ""
        task_intent = self._classify_task_intent(task)
        task_requires_action = task_intent in {"edit", "implement"}

        try:
            self._emit_status("running", task)

            for turn_idx in range(self.definition.max_turns):
                if self.cancel_event is not None and self.cancel_event.is_set():
                    final_text = final_text or "Cancelled by user before completion."
                    final_status = "cancelled"
                    self._emit_status("cancelled", "cancelled by user")
                    break
                self._emit_status("thinking", f"turn {turn_idx + 1}")

                assistant_msg, raw_usage = self.backend.chat_completion(
                    messages=self.messages,
                    tools=self.tools.definitions(),
                    max_tokens=self.config.max_output_tokens,
                )

                self.usage = self.usage.add_turn(
                    raw_usage.get("prompt_tokens", 0),
                    raw_usage.get("completion_tokens", 0),
                )
                self.messages.append(assistant_msg)

                if not assistant_msg.tool_calls:
                    final_text = assistant_msg.content or ""
                    if (
                        task_requires_action
                        and not self._has_concrete_action(all_tool_calls)
                        and not self._response_describes_blocker(final_text)
                        and turn_idx + 1 < self.definition.max_turns
                    ):
                        self.messages.append(Message(
                            role="user",
                            content=(
                                "Action is still required. You have not used a write or execution tool yet. "
                                "Do not stop. Make the change with file_edit, file_write, or bash, "
                                "or explain the concrete blocker."
                            ),
                        ))
                        continue
                    self._emit_status("completed", final_text)
                    break

                stop_during_tools_status: str | None = None
                for tc in assistant_msg.tool_calls:
                    self._emit_tool_event(tc, "running")
                    if self.cancel_event is not None and self.cancel_event.is_set():
                        content = "Tool execution skipped because the user cancelled the agent."
                        all_tool_calls.append(tc.name)
                        self.messages.append(Message(
                            role="tool",
                            content=content,
                            tool_call_id=tc.id,
                            name=tc.name,
                        ))
                        self._emit_tool_event(
                            tc,
                            "cancelled",
                            is_error=True,
                            error_code="CANCELLED_BY_USER",
                        )
                        final_text = final_text or "Cancelled by user before completion."
                        final_status = "cancelled"
                        stop_during_tools_status = "cancelled"
                        break

                    args_fingerprint = str(sorted(tc.arguments.items())) if isinstance(tc.arguments, dict) else str(tc.arguments)
                    call_key = (tc.name, args_fingerprint)
                    repeat_call_count = sum(1 for c in self._recent_calls if c == call_key)
                    if repeat_call_count >= 2:
                        content = (
                            f"BLOCKED: This exact {tc.name} call has already been made {repeat_call_count} times. "
                            f"Use file_edit, file_write, bash, or explain the blocker instead."
                        )
                        self.messages.append(Message(
                            role="tool",
                            content=content,
                            tool_call_id=tc.id,
                            name=tc.name,
                        ))
                        all_tool_calls.append(tc.name)
                        self._emit_tool_event(
                            tc,
                            "failed",
                            is_error=True,
                            error_code="DUPLICATE_TOOL_CALL",
                        )
                        continue

                    # Malformed-arguments guard: the backend tags unparseable
                    # tool-call JSON instead of fabricating parameters. Tell the
                    # agent exactly that so it resends valid JSON rather than
                    # looping into per-tool failure blocks.
                    if isinstance(tc.arguments, dict) and MALFORMED_ARGS_KEY in tc.arguments:
                        raw_snippet = str(tc.arguments[MALFORMED_ARGS_KEY])[:300]
                        content = (
                            f"TOOL ARGUMENTS NOT PARSED: the arguments for `{tc.name}` were "
                            f"not valid JSON, so the call was NOT executed. Resend the same "
                            f"`{tc.name}` call with a single well-formed JSON object "
                            f"(double-quoted keys and strings, newlines escaped as \\n). "
                            f"Raw arguments received: {raw_snippet}"
                        )
                        self._recent_calls.append(call_key)
                        if len(self._recent_calls) > 20:
                            self._recent_calls = self._recent_calls[-20:]
                        all_tool_calls.append(tc.name)
                        self.messages.append(Message(
                            role="tool",
                            content=content,
                            tool_call_id=tc.id,
                            name=tc.name,
                        ))
                        self._emit_tool_event(
                            tc,
                            "failed",
                            is_error=True,
                            error_code="MALFORMED_ARGUMENTS",
                        )
                        continue

                    denial = self.permissions.blocks(tc.name)
                    if denial:
                        content = f"Permission denied: {denial.reason}"
                        terminal_status = "denied"
                        terminal_error = True
                        terminal_error_code = "PERMISSION_DENIED"
                    else:
                        tool = self.tools.get(tc.name)
                        if tool:
                            approval_result = self._check_approval(tc.name, tc.arguments)
                            if approval_result is not None:
                                content, terminal_error, terminal_status, terminal_error_code = approval_result
                                if terminal_error:
                                    self._recent_errors.append((tc.name, content))
                            else:
                                if self.on_tool_call:
                                    try:
                                        self.on_tool_call(self.display_label, tc.name, tc.arguments)
                                    except Exception:
                                        pass
                                try:
                                    result = tool.execute_with_cancel(tc.arguments, self.cancel_event)
                                except Exception:
                                    self._emit_tool_event(
                                        tc,
                                        "failed",
                                        is_error=True,
                                        error_code="TOOL_EXCEPTION",
                                    )
                                    raise
                                content = result.content
                                if self.cancel_event is not None and self.cancel_event.is_set():
                                    terminal_status = "cancelled"
                                    terminal_error = True
                                    terminal_error_code = "CANCELLED_BY_USER"
                                elif result.is_error:
                                    terminal_status = "failed"
                                    terminal_error = True
                                    terminal_error_code = result.error_code or "TOOL_ERROR"
                                else:
                                    terminal_status = "completed"
                                    terminal_error = False
                                    terminal_error_code = ""
                                if result.is_error:
                                    error_key = (tc.name, result.content)
                                    repeat_error_count = sum(1 for e in self._recent_errors if e == error_key)
                                    self._recent_errors.append(error_key)
                                    if repeat_error_count >= 1:
                                        content += (
                                            "\n\nThis error already happened before. "
                                            "Try a different tool or different arguments."
                                        )
                                else:
                                    self._recent_errors = [e for e in self._recent_errors if e[0] != tc.name]
                        else:
                            content = f"Unknown tool: {tc.name}"
                            terminal_status = "failed"
                            terminal_error = True
                            terminal_error_code = "UNKNOWN_TOOL"

                    self._recent_calls.append(call_key)
                    if len(self._recent_calls) > 20:
                        self._recent_calls = self._recent_calls[-20:]
                    all_tool_calls.append(tc.name)
                    self.messages.append(Message(
                        role="tool",
                        content=content,
                        tool_call_id=tc.id,
                        name=tc.name,
                    ))
                    self._emit_tool_event(
                        tc,
                        terminal_status,
                        is_error=terminal_error,
                        error_code=terminal_error_code,
                    )

                    if terminal_status == "cancelled":
                        final_text = final_text or "Cancelled by user before completion."
                        final_status = "cancelled"
                        stop_during_tools_status = "cancelled"
                        break
                    if terminal_error_code.startswith("APPROVAL_DENIED"):
                        final_text = "Stopped because the user denied approval."
                        final_status = "approval_denied"
                        final_error_code = terminal_error_code
                        stop_during_tools_status = "approval_denied"
                        break

                if stop_during_tools_status:
                    self._emit_status(
                        stop_during_tools_status,
                        "cancelled by user"
                        if stop_during_tools_status == "cancelled"
                        else "approval denied",
                    )
                    break
            else:
                # Loop exhausted while the model was still calling tools:
                # report that instead of an empty "completed" result.
                if not final_text:
                    final_text = (
                        f"Stopped: reached the {self.definition.max_turns}-turn limit "
                        "before producing a final answer."
                    )
                final_status = "max_turns"
                self._emit_status("max_turns", "turn limit reached")

            return AgentResult(
                agent_name=self.definition.name,
                agent_index=self.agent_index,
                display_label=self.display_label,
                output=final_text,
                task=task,
                tool_calls_made=all_tool_calls,
                usage=self.usage,
                status=final_status,
                error_code=final_error_code,
            )
        except Exception as exc:
            self._emit_status("failed", "agent failed")
            return AgentResult(
                agent_name=self.definition.name,
                agent_index=self.agent_index,
                display_label=self.display_label,
                output="Agent failed before completing the task.",
                task=task,
                tool_calls_made=all_tool_calls,
                usage=self.usage,
                status="failed",
                error=str(exc),
            )

    def _emit_status(self, status: str, detail: str) -> None:
        if not self.on_status:
            return
        try:
            self.on_status(self.display_label, status, detail)
        except Exception:
            pass

    def _emit_tool_event(
        self,
        tool_call,
        status: str,
        *,
        is_error: bool = False,
        error_code: str = "",
    ) -> None:
        if not self.on_tool_event:
            return
        arguments = dict(tool_call.arguments) if isinstance(tool_call.arguments, dict) else {}
        try:
            self.on_tool_event(AgentToolEvent(
                tool_call_id=tool_call.id,
                tool_name=tool_call.name,
                status=status,
                arguments=arguments,
                is_error=is_error,
                error_code=error_code,
            ))
        except Exception:
            pass

    @staticmethod
    def _classify_task_intent(task: str) -> str:
        text = (task or "").strip().lower()
        if not text:
            return "question"

        review_patterns = (
            r"\breview\b",
            r"\baudit\b",
            r"\binspect\b",
            r"\blook\s+over\b",
            r"\bcode\s+review\b",
        )
        question_prefixes = (
            "what", "why", "how", "explain", "summarize", "show me", "tell me",
        )
        investigation_patterns = (
            r"\bfind\b",
            r"\bdebug\b",
            r"\binvestigate\b",
            r"\btrace\b",
            r"\bfigure out\b",
            r"\bwhy is\b",
            r"\bwhat is wrong with\b",
        )
        edit_patterns = (
            r"\bfix\b",
            r"\bupdate\b",
            r"\bedit\b",
            r"\bchange\b",
            r"\brewrite\b",
            r"\brefactor\b",
            r"\bpatch\b",
            r"\bmodify\b",
            r"\bimprove\b",
            r"\bpolish\b",
            r"\btighten\b",
            r"\bclean up\b",
            r"\baddress\b",
            r"\bcorrect\b",
        )
        implement_patterns = (
            r"\bimplement\b",
            r"\bbuild\b",
            r"\bcreate\b",
            r"\badd\b",
            r"\bremove\b",
            r"\brename\b",
            r"\bwire up\b",
            r"\bintegrate\b",
            r"\bmake\s+it\b",
        )

        if any(text.startswith(prefix) for prefix in question_prefixes):
            return "question"
        if any(re.search(pattern, text) for pattern in review_patterns):
            return "review"
        if any(re.search(pattern, text) for pattern in investigation_patterns):
            return "investigate"
        if any(re.search(pattern, text) for pattern in edit_patterns):
            return "edit"
        if any(re.search(pattern, text) for pattern in implement_patterns):
            return "implement"
        return "question"

    @staticmethod
    def _has_concrete_action(tool_calls: list[str]) -> bool:
        return any(name in {"file_edit", "file_write", "bash"} for name in tool_calls)

    @staticmethod
    def _response_describes_blocker(text: str) -> bool:
        lowered = (text or "").lower()
        blocker_markers = (
            "blocked", "cannot", "can't", "unable", "permission denied",
            "missing", "not found", "failed", "need approval", "waiting for",
        )
        return any(marker in lowered for marker in blocker_markers)

    def _check_approval(
        self,
        tool_name: str,
        arguments: dict,
    ) -> tuple[str, bool, str, str] | None:
        """Return a synthetic tool result when approval blocks execution."""
        if not self.permissions.needs_approval(tool_name):
            return None

        approval = self._request_approval(tool_name, arguments)
        if approval == APPROVAL_DENIED_SIGNAL:
            return (
                "Tool execution skipped because the user denied approval.",
                True,
                "denied",
                "APPROVAL_DENIED",
            )
        if self.cancel_event is not None and self.cancel_event.is_set():
            return (
                "Tool execution skipped because the user cancelled the agent.",
                True,
                "cancelled",
                "CANCELLED_BY_USER",
            )
        if approval is False:
            return (
                "Tool execution skipped because the user denied approval.",
                True,
                "denied",
                "APPROVAL_DENIED",
            )
        if approval == "ALL":
            self.permissions.grant_session_auto_approve(tool_name)
            return None
        if isinstance(approval, str):
            return (
                f"Tool execution skipped because the user declined approval and wrote: {approval!r}",
                True,
                "denied",
                "APPROVAL_DENIED_WITH_FEEDBACK",
            )
        return None

    def _request_approval(self, tool_name: str, arguments: dict) -> bool | str:
        if self.on_approval_request:
            return self.on_approval_request(tool_name, arguments)
        return False
