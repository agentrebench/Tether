"""Core data models for Tether."""
from __future__ import annotations

from dataclasses import dataclass, field

import json

@dataclass(frozen=True)
class UsageSummary:
    input_tokens: int = 0
    output_tokens: int = 0

    def add_turn(self, input_tokens: int, output_tokens: int) -> UsageSummary:
        return UsageSummary(
            self.input_tokens + input_tokens,
            self.output_tokens + output_tokens,
        )

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True)
class PermissionDenial:
    tool_name: str
    reason: str


@dataclass(frozen=True)
class StreamEvent:
    """Unified event from a streaming chat completion.

    type:
      - "text": new assistant text chunk (delta). `text` holds the delta.
      - "thinking": reasoning-channel delta (DeepSeek/o1-style). Optional UI.
      - "tool_running": a tool is in flight. `tool_name` + `tool_args_preview` set.
      - "tool_done": tool finished. `tool_name` + optional `tool_output_preview`.
      - "checkpoint": the tool-cycle safety cap fired and a final summary is being prepared.
      - "complete": stream ended. No payload.
    """
    type: str
    text: str = ""
    tool_name: str = ""
    tool_args_preview: str = ""
    tool_output_preview: str = ""
    is_error: bool = False
    tool_call_id: str = ""
    display_kind: str = "generic"
    error_code: str = ""
    metadata: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    parameters: dict  # JSON Schema


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass(frozen=True)
class ToolResult:
    name: str
    content: str
    tool_call_id: str = ""
    is_error: bool = False
    display_kind: str = "generic"
    error_code: str = ""
    metadata: dict = field(default_factory=dict)


@dataclass
class Message:
    role: str  # system, user, assistant, tool
    content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None
    name: str | None = None
    # DeepSeek thinking-mode API requires the previous turn's reasoning trace
    # to be replayed on every assistant message (tool-call or plain text),
    # otherwise it 400s with "the reasoning_content in the thinking mode must
    # be passed back to the api."
    reasoning_content: str | None = None

    def to_api_dict(self) -> dict:
        d: dict = {"role": self.role}
        tool_calls_list = None
        if self.tool_calls:
            tool_calls_list = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.name,
                        "arguments": json.dumps(tc.arguments) if isinstance(tc.arguments, dict) else tc.arguments,
                    },
                }
                for tc in self.tool_calls
            ]
        if self.content is not None:
            d["content"] = self.content
        elif self.role == "assistant":
            # llama-server requires the content key on assistant messages.
            # DeepSeek rejects content=null with no tool_calls ("content or
            # tool_calls must be set") — send "" in that case so both backends
            # accept it. When tool_calls are present, null is fine.
            d["content"] = None if tool_calls_list else ""
        if tool_calls_list:
            d["tool_calls"] = tool_calls_list
        if self.role == "assistant":
            # DeepSeek thinking-mode rejects assistant turns missing this
            # field entirely. Emit it always (empty string when the model
            # produced no visible reasoning, e.g. a fast tool call, or for
            # synthetic assistant messages from compaction).
            d["reasoning_content"] = self.reasoning_content or ""
        if self.tool_call_id is not None:
            d["tool_call_id"] = self.tool_call_id
        if self.name is not None:
            d["name"] = self.name
        return d

    def to_dict(self) -> dict:
        """Lossless storage form for session persistence. Unlike
        ``to_api_dict`` (API-shaped: stringified arguments, backend quirks),
        this round-trips through :meth:`from_dict` unchanged."""
        return {
            "role": self.role,
            "content": self.content,
            "tool_calls": [
                {"id": tc.id, "name": tc.name, "arguments": tc.arguments}
                for tc in self.tool_calls
            ],
            "tool_call_id": self.tool_call_id,
            "name": self.name,
            "reasoning_content": self.reasoning_content,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Message":
        return cls(
            role=data.get("role", "user"),
            content=data.get("content"),
            tool_calls=[
                ToolCall(
                    id=tc.get("id", ""),
                    name=tc.get("name", ""),
                    arguments=tc.get("arguments") or {},
                )
                for tc in (data.get("tool_calls") or [])
            ],
            tool_call_id=data.get("tool_call_id"),
            name=data.get("name"),
            reasoning_content=data.get("reasoning_content"),
        )


