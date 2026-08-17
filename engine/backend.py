"""OpenAI-compatible backend — talks to vLLM, llama-server, or a remote API."""
from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.request
import urllib.error
from dataclasses import dataclass
from typing import Generator

from ..core.config import TetherConfig
from ..core.logging import get_logger
from ..core.models import Message, StreamEvent, ToolCall, ToolDefinition
from .codex_backend import CodexExecBackend


# Transient failures worth retrying: rate limits, server-side errors, timeouts.
# 4xx client errors (bad request, auth) are NOT here — retrying them just
# repeats the same rejection. Context-overflow 400s have their own recovery
# path (compaction) in the engine.
RETRYABLE_HTTP_CODES = {408, 429, 500, 502, 503, 504}
RETRY_ATTEMPTS = 4
RETRY_BASE_DELAY = 1.0
RETRY_MAX_DELAY = 15.0


# Sentinel key marking tool-call arguments the model emitted as invalid JSON.
# It is carried through to the engine so it can return a precise "your JSON did
# not parse — resend valid JSON" correction, instead of silently handing a
# tool garbage (the old {"raw": ...} behaviour) and surfacing a misleading
# "missing required parameter" failure that the model can't diagnose.
MALFORMED_ARGS_KEY = "__unparsed_tool_args__"


def coerce_tool_arguments(raw) -> dict:
    """Best-effort parse of a tool-call arguments blob into a dict.

    Models occasionally emit arguments that aren't strict JSON (single quotes,
    Python literals, trailing commas) or that get truncated mid-object when the
    completion hits the output-token cap. We try a few lenient recoveries before
    giving up; on total failure we return a sentinel dict carrying the raw text
    under MALFORMED_ARGS_KEY so the caller can ask the model to resend it.
    """
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    text = raw.strip() if isinstance(raw, str) else raw
    if not text:
        return {}
    # 1. Strict JSON — the common, correct case.
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else {MALFORMED_ARGS_KEY: str(text)}
    except (json.JSONDecodeError, TypeError):
        pass
    # 2. Python-literal style: single-quoted keys, True/False/None, tuples.
    try:
        import ast

        parsed = ast.literal_eval(text)
        if isinstance(parsed, dict):
            return parsed
    except (ValueError, SyntaxError, TypeError):
        pass
    # 3. Give up, but preserve the raw text for an actionable error upstream.
    return {MALFORMED_ARGS_KEY: str(text)}


@dataclass
class InferenceBackend:
    config: TetherConfig
    logger: logging.Logger = None

    def __post_init__(self):
        if self.logger is None:
            self.logger = get_logger(__name__)

    @property
    def base_url(self) -> str:
        return self.config.server_url

    @property
    def chat_completions_url(self) -> str:
        base = self.base_url.rstrip("/")
        # OpenAI-compatible providers do not all use /v1.  Z.AI, for example,
        # exposes /v4/chat/completions. Respect a versioned base supplied by a
        # preset or custom provider instead of producing /v4/v1/...
        if re.search(r"/v\d+(?:beta\d*)?$", base):
            return f"{base}/chat/completions"
        return f"{base}/v1/chat/completions"

    def _request_headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.config.is_codex:
            return headers
        if self.config.is_remote:
            # Resolution order: env var first (recommended — never on disk),
            # then the active provider's stored key (convenience fallback).
            key = ""
            for env_name in self.config.api_key_env_names():
                key = os.environ.get(env_name, "").strip()
                if key:
                    break
            if not key:
                key = self.config.stored_api_key()
            if key:
                headers["Authorization"] = f"Bearer {key}"
            else:
                where = (
                    f"${self.config.api_key_env} (env) or stored api_key"
                    if self.config.api_key_env
                    else "stored api_key"
                )
                self.logger.warning(
                    f"Remote provider '{self.config.provider}' configured but no "
                    f"key in {where} — request will be unauthenticated"
                )
        return headers

    def _model_name(self) -> str:
        if self.config.is_remote and self.config.api_model:
            return self.config.api_model
        return "tether"

    def health_check(self) -> bool:
        if self.config.is_codex:
            return CodexExecBackend(self.config, self.logger).health_check()
        # Remote providers don't expose a /health endpoint — assume reachable
        # and let the first real request surface any auth/network errors.
        if self.config.is_remote:
            return True
        try:
            req = urllib.request.Request(f"{self.base_url}/health")
            with urllib.request.urlopen(req, timeout=3) as resp:
                if resp.status == 200:
                    self.logger.debug("Health check passed")
                    return True
                else:
                    self.logger.warning(f"Health check returned status {resp.status}")
                    return False
        except urllib.error.HTTPError as e:
            self.logger.warning(f"Health check HTTP error: {e.code} {e.reason}")
            return False
        except (urllib.error.URLError, OSError) as e:
            self.logger.warning(f"Health check failed to connect: {e}")
            return False

    def _urlopen_retry(self, req: urllib.request.Request, timeout: float):
        """urlopen with exponential backoff on transient failures.

        Retries 408/429/5xx and connection-level errors (URLError/OSError)
        before the response is opened; once urlopen returns, the body/stream
        is the caller's — a drop mid-stream is not replayable and never
        retried here. Honors a numeric Retry-After header when present.
        """
        delay = RETRY_BASE_DELAY
        for attempt in range(1, RETRY_ATTEMPTS + 1):
            try:
                return urllib.request.urlopen(req, timeout=timeout)
            except urllib.error.HTTPError as e:
                if e.code not in RETRYABLE_HTTP_CODES or attempt == RETRY_ATTEMPTS:
                    raise
                # Header peek only — leave the body unread so the final
                # attempt's handler can still read it for error logs.
                retry_after = (e.headers.get("Retry-After") or "").strip()
                wait = delay
                if retry_after.isdigit():
                    wait = min(float(retry_after), RETRY_MAX_DELAY)
                self.logger.warning(
                    f"HTTP {e.code} from server — retrying in {wait:.1f}s "
                    f"(attempt {attempt}/{RETRY_ATTEMPTS})"
                )
            except urllib.error.URLError as e:
                if attempt == RETRY_ATTEMPTS:
                    raise
                wait = delay
                self.logger.warning(
                    f"Connection error ({e.reason}) — retrying in {wait:.1f}s "
                    f"(attempt {attempt}/{RETRY_ATTEMPTS})"
                )
            except OSError as e:
                if attempt == RETRY_ATTEMPTS:
                    raise
                wait = delay
                self.logger.warning(
                    f"Network error ({e}) — retrying in {wait:.1f}s "
                    f"(attempt {attempt}/{RETRY_ATTEMPTS})"
                )
            time.sleep(wait)
            delay = min(delay * 2, RETRY_MAX_DELAY)

    def _build_payload(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None,
        temperature: float | None,
        max_tokens: int,
        stream: bool,
    ) -> dict:
        tokens_field = getattr(self.config, "max_tokens_field", "") or "max_tokens"
        api_messages = []
        for message in messages:
            item = message.to_api_dict()
            # reasoning_content is a DeepSeek/Kimi extension. Sending it to
            # OpenAI, Anthropic compatibility, GLM, or a generic endpoint can
            # turn otherwise valid history into a 400 unknown-field response.
            if self.config.is_remote and self.config.provider not in {"deepseek", "kimi"}:
                item.pop("reasoning_content", None)
            api_messages.append(item)
        payload: dict = {
            "model": self._model_name(),
            "messages": api_messages,
            tokens_field: max_tokens,
            "stream": stream,
        }
        # Thinking-mode models reject temperature/top_p; instead they take an
        # optional reasoning_effort knob. OpenAI's gpt-5.x
        # and o-series families also reject any non-default sampling values,
        # so they set omit_sampling=True and we just drop the fields.
        # A transient override (set by the engine for plan mode) wins over the
        # configured effort. Only ever non-empty when the provider already
        # supports reasoning_effort, so sampling-only models stay untouched.
        effort = getattr(self, "reasoning_effort_override", "") or self.config.reasoning_effort
        if effort:
            payload["reasoning_effort"] = effort
        elif not getattr(self.config, "omit_sampling", False):
            payload["temperature"] = temperature or self.config.temperature
            payload["top_p"] = self.config.top_p
        thinking_mode = getattr(self.config, "thinking_mode", "")
        # Kimi K3/K2.7 have fixed thinking behavior; their API does not need a
        # toggle. K2.6, DeepSeek, and GLM accept the OpenAI-compatible object.
        supports_thinking_toggle = (
            self.config.provider in {"deepseek", "glm"}
            or (
                self.config.provider == "kimi"
                and self.config.api_model == "kimi-k2.6"
            )
        )
        if thinking_mode and supports_thinking_toggle:
            payload["thinking"] = {"type": thinking_mode}
        if tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.parameters,
                    },
                }
                for t in tools
            ]
        return payload

    @staticmethod
    def _parse_tool_calls(raw_tool_calls: list[dict]) -> list[ToolCall]:
        tool_calls = []
        for tc in raw_tool_calls:
            func = tc["function"]
            args = coerce_tool_arguments(func.get("arguments", "{}"))
            tool_calls.append(ToolCall(
                id=tc.get("id", f"call_{len(tool_calls)}"),
                name=func["name"],
                arguments=args,
            ))
        return tool_calls

    def chat_completion(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        temperature: float | None = None,
        max_tokens: int = 4096,
    ) -> tuple[Message, dict]:
        """Non-streaming. Returns (assistant_message, usage_dict)."""
        if self.config.is_codex:
            return CodexExecBackend(self.config, self.logger).chat_completion(
                messages=messages,
                tools=tools,
                temperature=temperature,
                max_tokens=max_tokens,
            )

        payload = self._build_payload(messages, tools, temperature, max_tokens, stream=False)
        body = json.dumps(payload).encode()
        req = urllib.request.Request(
            self.chat_completions_url,
            data=body,
            headers=self._request_headers(),
            method="POST",
        )

        self.logger.debug(f"Sending chat completion request with {len(messages)} messages")
        try:
            with self._urlopen_retry(req, timeout=600) as resp:
                data = json.loads(resp.read())
            self.logger.debug("Received chat completion response")
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            self.logger.error(
                f"HTTP error in chat completion: {e.code} {e.reason} | body: {body[:1000]}"
            )
            e.tether_body = body  # the stream is consumed; expose it to callers
            # Re-raise as a fresh HTTPError-like exception so callers still see the code,
            # but make sure the body is preserved on the exception for upstream logs.
            try:
                e.add_note(f"body: {body[:1000]}")  # 3.11+
            except AttributeError:
                pass
            raise
        except urllib.error.URLError as e:
            self.logger.error(f"URL error in chat completion: {e.reason}")
            raise
        except OSError as e:
            self.logger.error(f"OS error in chat completion: {e}")
            raise

        choice = data["choices"][0]
        msg = choice["message"]
        usage = data.get("usage", {})

        tool_calls = self._parse_tool_calls(msg.get("tool_calls") or [])
        assistant_msg = Message(
            role="assistant",
            content=msg.get("content"),
            tool_calls=tool_calls,
            reasoning_content=msg.get("reasoning_content"),
        )
        return assistant_msg, usage

    def chat_completion_streaming(
        self,
        messages: list[Message],
        on_event,  # Callable[[StreamEvent], None]
        tools: list[ToolDefinition] | None = None,
        temperature: float | None = None,
        max_tokens: int = 4096,
        cancel_event=None,  # threading.Event | None
    ) -> tuple[Message, dict]:
        """Unified streaming entrypoint.

        Codex emits whole `agent_message` items + tool-call progress.
        Non-codex emits token-level text deltas plus tool-call streaming.
        Both surface as `StreamEvent`s on the same callback. Passing a
        `cancel_event` interrupts cleanly between SSE chunks or codex lines.
        """
        if self.config.is_codex:
            return CodexExecBackend(self.config, self.logger).chat_completion_streaming(
                messages=messages,
                on_event=on_event,
                tools=tools,
                temperature=temperature,
                max_tokens=max_tokens,
                cancel_event=cancel_event,
            )

        def _on_text(text: str) -> None:
            on_event(StreamEvent(type="text", text=text))

        def _on_thinking(text: str) -> None:
            on_event(StreamEvent(type="thinking", text=text))

        def _on_tool_start(name: str) -> None:
            on_event(StreamEvent(type="tool_running", tool_name=name))

        # The SSE path doesn't natively respect cancel; wrap it by yielding
        # through a cancel check on every chunk. chat_completion_stream_parsed
        # already iterates chunks, so we monkey-bridge via a thin wrapper.
        if cancel_event is None:
            return self.chat_completion_stream_parsed(
                messages=messages, tools=tools,
                temperature=temperature, max_tokens=max_tokens,
                on_text_chunk=_on_text, on_thinking_chunk=_on_thinking,
                on_tool_call_start=_on_tool_start,
            )

        # chat_completion_stream_parsed checks cancel_event on every SSE
        # event, so tool-argument and reasoning-only streams cancel promptly
        # too (a text-chunk-only check would miss them).
        return self.chat_completion_stream_parsed(
            messages=messages, tools=tools,
            temperature=temperature, max_tokens=max_tokens,
            on_text_chunk=_on_text, on_thinking_chunk=_on_thinking,
            on_tool_call_start=_on_tool_start,
            cancel_event=cancel_event,
        )

    def chat_completion_stream(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        temperature: float | None = None,
        max_tokens: int = 4096,
    ) -> Generator[dict, None, None]:
        """Stream a chat completion. Yields SSE chunk dicts from the server."""
        if self.config.is_codex:
            msg, usage = self.chat_completion(messages, tools, temperature, max_tokens)
            yield {
                "choices": [
                    {
                        "delta": {"content": msg.content or ""},
                        "finish_reason": "stop",
                    }
                ],
                "usage": usage,
            }
            return

        payload = self._build_payload(messages, tools, temperature, max_tokens, stream=True)
        body = json.dumps(payload).encode()
        req = urllib.request.Request(
            self.chat_completions_url,
            data=body,
            headers=self._request_headers(),
            method="POST",
        )

        self.logger.debug(f"Starting chat completion stream with {len(messages)} messages")
        try:
            with self._urlopen_retry(req, timeout=600) as resp:
                self.logger.debug("Chat completion stream connected")
                # HTTPResponse.read(4096) waits for a full block on many
                # servers. Small SSE deltas could therefore sit invisibly in
                # the socket buffer until several kilobytes accumulated. SSE
                # is line framed, so consume it line-by-line and yield the
                # first token as soon as its newline arrives.
                for raw_line in resp:
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line or not line.startswith("data:"):
                        continue
                    data_str = line[5:].lstrip()
                    if data_str == "[DONE]":
                        self.logger.debug("Chat completion stream ended")
                        return
                    try:
                        yield json.loads(data_str)
                    except json.JSONDecodeError:
                        self.logger.warning(
                            f"Failed to decode JSON from stream: {data_str[:100]}..."
                        )
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            self.logger.error(
                f"HTTP error in chat completion stream: {e.code} {e.reason} | body: {body[:1000]}"
            )
            e.tether_body = body  # the stream is consumed; expose it to callers
            try:
                e.add_note(f"body: {body[:1000]}")
            except AttributeError:
                pass
            raise
        except urllib.error.URLError as e:
            self.logger.error(f"URL error in chat completion stream: {e.reason}")
            raise
        except OSError as e:
            self.logger.error(f"OS error in chat completion stream: {e}")
            raise

    def chat_completion_stream_parsed(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        temperature: float | None = None,
        max_tokens: int = 4096,
        on_text_chunk: callable = None,
        on_thinking_chunk: callable = None,
        on_tool_call_start: callable = None,
        on_tool_call_args_chunk: callable = None,
        on_tool_call_named: callable = None,  # (index, name) — pairs args chunks with a call
        cancel_event=None,  # threading.Event | None — interrupt between chunks
    ) -> tuple[Message, dict]:
        """Stream with callbacks for live output. Returns final (Message, usage).

        When `cancel_event` is supplied it is polled before each SSE chunk is
        processed; if set, streaming stops immediately and CancelledByUser is
        raised so the caller can abandon the turn cleanly."""
        content_pieces: list[str] = []
        thinking_pieces: list[str] = []
        tool_calls_building: dict[int, dict] = {}  # index -> {id, name, args_str}
        usage = {}
        finish_reason = None

        for chunk in self.chat_completion_stream(messages, tools, temperature, max_tokens):
            if cancel_event is not None and cancel_event.is_set():
                from .codex_backend import CancelledByUser
                raise CancelledByUser("streaming cancelled by user")
            if "usage" in chunk:
                usage = chunk["usage"]

            for choice in chunk.get("choices", []):
                delta = choice.get("delta", {})
                finish_reason = choice.get("finish_reason") or finish_reason

                # Text content
                if delta.get("content"):
                    text = delta["content"]
                    content_pieces.append(text)
                    if on_text_chunk:
                        on_text_chunk(text)

                # Thinking / reasoning content (some models emit this)
                if delta.get("reasoning_content"):
                    text = delta["reasoning_content"]
                    thinking_pieces.append(text)
                    if on_thinking_chunk:
                        on_thinking_chunk(text)

                # Tool calls (streamed incrementally)
                if delta.get("tool_calls"):
                    for tc_delta in delta["tool_calls"]:
                        idx = tc_delta.get("index", 0)
                        if idx not in tool_calls_building:
                            tool_calls_building[idx] = {
                                "id": tc_delta.get("id", f"call_{idx}"),
                                "name": "",
                                "args_str": "",
                            }
                        entry = tool_calls_building[idx]

                        if tc_delta.get("id"):
                            entry["id"] = tc_delta["id"]

                        func = tc_delta.get("function", {})
                        if func.get("name"):
                            entry["name"] = func["name"]
                            if on_tool_call_named:
                                on_tool_call_named(idx, entry["name"])
                            if on_tool_call_start:
                                on_tool_call_start(entry["name"])

                        if func.get("arguments"):
                            entry["args_str"] += func["arguments"]
                            if on_tool_call_args_chunk:
                                on_tool_call_args_chunk(idx, func["arguments"])
        # End of stream loop — also surface usage-cost in logs at debug.

        # Build final message
        tool_calls = []
        for idx in sorted(tool_calls_building.keys()):
            entry = tool_calls_building[idx]
            args = coerce_tool_arguments(entry["args_str"])
            tool_calls.append(ToolCall(
                id=entry["id"],
                name=entry["name"],
                arguments=args,
            ))

        content = "".join(content_pieces) or None
        reasoning = "".join(thinking_pieces) or None
        assistant_msg = Message(
            role="assistant",
            content=content,
            tool_calls=tool_calls,
            reasoning_content=reasoning,
        )
        return assistant_msg, usage
