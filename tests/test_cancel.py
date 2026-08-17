"""Tests for cooperative turn cancellation in QueryEngine.submit (feature #1/#2)."""
import contextlib
import io
import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from tether.core.config import TetherConfig
from tether.core.permissions import APPROVAL_DENIED_SIGNAL, PermissionContext
from tether.core.models import Message, ToolCall, ToolResult
from tether.engine.query_engine import QueryEngine
from tether.engine.codex_backend import CancelledByUser
from tether.tools.base import BaseTool, ToolRegistry


def _engine():
    cfg = TetherConfig(provider="local")
    eng = QueryEngine(cfg, ToolRegistry.build_default(), PermissionContext.from_config(cfg))
    eng._should_continue_after_text_response = lambda *a, **k: False
    return eng


class RecordingTool(BaseTool):
    def __init__(self, name, on_execute=None, *, error_code=""):
        self._name = name
        self.on_execute = on_execute
        self.error_code = error_code
        self.calls = []

    @property
    def name(self):
        return self._name

    @property
    def description(self):
        return f"Test tool {self._name}"

    @property
    def parameters(self):
        return {"type": "object", "properties": {}}

    def execute(self, arguments):
        self.calls.append(arguments)
        if self.on_execute is not None:
            self.on_execute()
        return ToolResult(
            name=self.name,
            content=f"ran {self.name}",
            is_error=bool(self.error_code),
            error_code=self.error_code,
        )


def _engine_with_tools(*tools):
    cfg = TetherConfig(provider="local")
    registry = ToolRegistry(tools={tool.name: tool for tool in tools})
    eng = QueryEngine(cfg, registry, PermissionContext.from_config(cfg))
    eng._should_continue_after_text_response = lambda *a, **k: False
    return eng


@contextlib.contextmanager
def _quiet():
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        yield


class Cancel(unittest.TestCase):
    def test_preset_cancel_returns_before_model_call(self):
        eng = _engine()
        called = {"n": 0}

        def fake(messages, tools, **kw):
            called["n"] += 1
            return Message(role="assistant", content="should not run"), {}

        eng.backend.chat_completion_stream_parsed = fake
        ev = threading.Event()
        ev.set()
        with _quiet():
            res = eng.submit("hi", cancel_event=ev)
        self.assertEqual(res.stop_reason, "cancelled")
        self.assertEqual(called["n"], 0)  # never called the model
        # history well-formed: user followed by synthetic assistant
        self.assertEqual(eng.messages[-1].role, "assistant")
        self.assertIn("interrupted", eng.messages[-1].content)

    def test_midstream_cancel_is_clean(self):
        eng = _engine()

        def fake(messages, tools, cancel_event=None, **kw):
            # simulate the user cancelling while tokens stream
            raise CancelledByUser("boom")

        eng.backend.chat_completion_stream_parsed = fake
        with _quiet():
            res = eng.submit("hi", cancel_event=threading.Event())
        self.assertEqual(res.stop_reason, "cancelled")
        self.assertEqual(eng.messages[-1].role, "assistant")

    def test_cancel_after_toolcalls_answers_pending_calls(self):
        eng = _engine()

        def fake(messages, tools, cancel_event=None, **kw):
            # model emits a tool call, but the user cancels during the stream
            cancel_event.set()
            tc = ToolCall(id="call_1", name="bash", arguments={"command": "sleep 100"})
            return Message(role="assistant", content="running", tool_calls=[tc]), {}

        eng.backend.chat_completion_stream_parsed = fake
        with _quiet():
            res = eng.submit("do it", cancel_event=threading.Event())
        self.assertEqual(res.stop_reason, "cancelled")
        # the dangling tool_call must be answered so the next turn isn't malformed
        tool_msgs = [m for m in eng.messages if m.role == "tool" and m.tool_call_id == "call_1"]
        self.assertEqual(len(tool_msgs), 1)
        self.assertIn("cancelled", tool_msgs[0].content)

    def test_no_cancel_event_runs_normally(self):
        eng = _engine()

        def fake(messages, tools, **kw):
            return Message(role="assistant", content="done"), {}

        eng.backend.chat_completion_stream_parsed = fake
        with _quiet():
            res = eng.submit("hi")  # no cancel_event
        self.assertEqual(res.stop_reason, "completed")

    def test_cancel_after_partial_batch_repairs_every_pending_tool_call(self):
        cancel = threading.Event()
        first = RecordingTool("first", on_execute=cancel.set)
        later = RecordingTool("later")
        agent = RecordingTool("agent")
        eng = _engine_with_tools(first, later, agent)
        eng.on_approval_request = lambda *_: True

        def fake(messages, tools, **kwargs):
            return Message(role="assistant", tool_calls=[
                ToolCall(id="call_first", name="first", arguments={}),
                ToolCall(id="call_later", name="later", arguments={}),
                ToolCall(id="call_agent", name="agent", arguments={}),
            ]), {}

        eng.backend.chat_completion_stream_parsed = fake
        with _quiet():
            res = eng.submit("run the batch", cancel_event=cancel)

        self.assertEqual(res.stop_reason, "cancelled")
        self.assertEqual(len(first.calls), 1)
        self.assertEqual(later.calls, [])
        self.assertEqual(agent.calls, [])
        tool_messages = [message for message in eng.messages if message.role == "tool"]
        self.assertEqual(
            [message.tool_call_id for message in tool_messages],
            ["call_first", "call_later", "call_agent"],
        )
        self.assertIn("cancelled", tool_messages[1].content)
        self.assertIn("cancelled", tool_messages[2].content)


class ApprovalCancellation(unittest.TestCase):
    def test_explicit_no_stops_batch_and_appends_prompt_without_model_retry(self):
        denied = RecordingTool("bash")
        later = RecordingTool("later")
        agent = RecordingTool("agent")
        eng = _engine_with_tools(denied, later, agent)
        events = []
        eng.on_stream_event = events.append
        eng.on_approval_request = lambda *_: False
        model_calls = {"count": 0}

        def fake(messages, tools, **kwargs):
            model_calls["count"] += 1
            return Message(role="assistant", tool_calls=[
                ToolCall(id="call_denied", name="bash", arguments={"command": "touch never"}),
                ToolCall(id="call_later", name="later", arguments={}),
                ToolCall(id="call_agent", name="agent", arguments={}),
            ]), {}

        eng.backend.chat_completion_stream_parsed = fake
        with _quiet():
            res = eng.submit("run the batch", cancel_event=threading.Event())

        expected = "I didn’t run that command. What would you like me to do instead?"
        self.assertEqual(res.stop_reason, "approval_denied")
        self.assertEqual(res.output, expected)
        self.assertEqual(model_calls["count"], 1)
        self.assertEqual(denied.calls, [])
        self.assertEqual(later.calls, [])
        self.assertEqual(agent.calls, [])
        self.assertEqual(eng._progress.bash_commands, [])
        tool_messages = [message for message in eng.messages if message.role == "tool"]
        self.assertEqual(
            [message.tool_call_id for message in tool_messages],
            ["call_denied", "call_later", "call_agent"],
        )
        self.assertTrue(all(message.content for message in tool_messages))
        self.assertEqual(eng.messages[-1].role, "assistant")
        self.assertEqual(eng.messages[-1].content, expected)
        denied_events = [
            event for event in events
            if event.type == "tool_done" and event.tool_call_id == "call_denied"
        ]
        self.assertEqual(denied_events[0].error_code, "APPROVAL_DENIED")

    def test_stop_wins_over_allow_or_false_release_before_execution(self):
        for approval_result in (True, False):
            with self.subTest(approval_result=approval_result):
                cancel = threading.Event()
                command = RecordingTool("dangerous")
                eng = _engine_with_tools(command)

                def approval(*_):
                    cancel.set()
                    return approval_result

                eng.on_approval_request = approval
                eng.backend.chat_completion_stream_parsed = lambda *args, **kwargs: (
                    Message(role="assistant", tool_calls=[
                        ToolCall(id="call_1", name="dangerous", arguments={}),
                    ]),
                    {},
                )
                with _quiet():
                    res = eng.submit("run it", cancel_event=cancel)

                self.assertEqual(res.stop_reason, "cancelled")
                self.assertEqual(command.calls, [])
                self.assertNotEqual(
                    res.output,
                    "I didn’t run that command. What would you like me to do instead?",
                )
                tool_message = next(message for message in eng.messages if message.role == "tool")
                self.assertIn("cancelled", tool_message.content.lower())

    def test_structured_no_signal_wins_over_cancel_and_stops_siblings(self):
        cancel = threading.Event()
        denied = RecordingTool("bash")
        later = RecordingTool("later")
        agent = RecordingTool("agent")
        eng = _engine_with_tools(denied, later, agent)

        def approval(*_):
            cancel.set()
            return APPROVAL_DENIED_SIGNAL

        eng.on_approval_request = approval
        eng.backend.chat_completion_stream_parsed = lambda *args, **kwargs: (
            Message(role="assistant", tool_calls=[
                ToolCall(id="call_denied", name="bash", arguments={"command": "touch never"}),
                ToolCall(id="call_later", name="later", arguments={}),
                ToolCall(id="call_agent", name="agent", arguments={}),
            ]),
            {},
        )
        with _quiet():
            res = eng.submit("run the batch", cancel_event=cancel)

        expected = "I didn’t run that command. What would you like me to do instead?"
        self.assertEqual(res.stop_reason, "approval_denied")
        self.assertEqual(res.output, expected)
        self.assertEqual(denied.calls, [])
        self.assertEqual(later.calls, [])
        self.assertEqual(agent.calls, [])
        tool_messages = [message for message in eng.messages if message.role == "tool"]
        self.assertEqual(
            [message.tool_call_id for message in tool_messages],
            ["call_denied", "call_later", "call_agent"],
        )

    def test_agent_approvals_are_serial_but_execution_remains_parallel(self):
        execution_lock = threading.Lock()
        execution_barrier = threading.Barrier(2)
        execution_active = {"value": 0, "maximum": 0}

        def execute_agent():
            with execution_lock:
                execution_active["value"] += 1
                execution_active["maximum"] = max(
                    execution_active["maximum"], execution_active["value"]
                )
            try:
                execution_barrier.wait(timeout=1)
            finally:
                with execution_lock:
                    execution_active["value"] -= 1

        agent = RecordingTool("agent", on_execute=execute_agent)
        eng = _engine_with_tools(agent)
        approval_lock = threading.Lock()
        approval_active = {"value": 0, "maximum": 0, "calls": 0}

        def approval(*_):
            with approval_lock:
                approval_active["value"] += 1
                approval_active["calls"] += 1
                approval_active["maximum"] = max(
                    approval_active["maximum"], approval_active["value"]
                )
            time.sleep(0.02)
            with approval_lock:
                approval_active["value"] -= 1
            return True

        eng.on_approval_request = approval
        model_calls = {"count": 0}

        def fake(messages, tools, **kwargs):
            model_calls["count"] += 1
            if model_calls["count"] == 1:
                return Message(role="assistant", tool_calls=[
                    ToolCall(id="agent_1", name="agent", arguments={"task": "one"}),
                    ToolCall(id="agent_2", name="agent", arguments={"task": "two"}),
                ]), {}
            return Message(role="assistant", content="done"), {}

        eng.backend.chat_completion_stream_parsed = fake
        with _quiet():
            res = eng.submit("delegate", cancel_event=threading.Event())

        self.assertEqual(res.stop_reason, "completed")
        self.assertEqual(approval_active["calls"], 2)
        self.assertEqual(approval_active["maximum"], 1)
        self.assertEqual(execution_active["maximum"], 2)
        self.assertEqual(len(agent.calls), 2)

    def test_nested_agent_denial_stops_outer_turn_before_another_model_call(self):
        agent = RecordingTool("agent", error_code="APPROVAL_DENIED")
        eng = _engine_with_tools(agent)
        eng.on_approval_request = lambda *_: True
        model_calls = {"count": 0}

        def fake(messages, tools, **kwargs):
            model_calls["count"] += 1
            if model_calls["count"] > 1:
                return Message(role="assistant", content="should not run"), {}
            return Message(role="assistant", tool_calls=[
                ToolCall(id="agent_1", name="agent", arguments={"task": "child work"}),
            ]), {}

        eng.backend.chat_completion_stream_parsed = fake
        with _quiet():
            res = eng.submit("delegate", cancel_event=threading.Event())

        expected = "I didn’t run that command. What would you like me to do instead?"
        self.assertEqual(res.stop_reason, "approval_denied")
        self.assertEqual(res.output, expected)
        self.assertEqual(model_calls["count"], 1)
        self.assertEqual(len(agent.calls), 1)
        self.assertEqual(eng.messages[-1].role, "assistant")
        self.assertEqual(eng.messages[-1].content, expected)


class TurnLimitCheckpoint(unittest.TestCase):
    def test_limit_gets_one_tool_free_checkpoint_call(self):
        eng = _engine()
        eng.config.max_turns = 0
        calls = []
        events = []
        eng.on_stream_event = events.append

        def fake(messages, tools, on_text_chunk=None, **kw):
            calls.append({"messages": messages, "tools": tools})
            if on_text_chunk:
                on_text_chunk("I changed the parser; tests still need to run.")
            return Message(
                role="assistant",
                content="I changed the parser; tests still need to run.",
            ), {"prompt_tokens": 7, "completion_tokens": 9}

        eng.backend.chat_completion_stream_parsed = fake
        with _quiet():
            res = eng.submit("fix the parser")

        self.assertEqual(res.stop_reason, "max_turns_reached")
        self.assertEqual(res.output, "I changed the parser; tests still need to run.")
        self.assertEqual(len(calls), 1)
        self.assertIsNone(calls[0]["tools"])
        self.assertIn("Tool use is disabled", calls[0]["messages"][-2].content)
        self.assertEqual(eng.messages[-1].role, "assistant")
        self.assertEqual(eng.usage.total, 16)
        self.assertIn("checkpoint", [event.type for event in events])

    def test_checkpoint_failure_returns_resumable_fallback(self):
        eng = _engine()
        eng.config.max_turns = 0

        def fake(*args, **kwargs):
            raise RuntimeError("provider unavailable")

        eng.backend.chat_completion_stream_parsed = fake
        with _quiet():
            res = eng.submit("fix the parser")

        self.assertEqual(res.stop_reason, "max_turns_reached")
        self.assertIn("safety limit", res.output)
        self.assertIn("Continue workflow", res.output)
        self.assertNotIn("(max turns reached)", res.output)

    def test_preset_cancel_skips_checkpoint_model_call(self):
        eng = _engine()
        eng.config.max_turns = 0
        called = {"value": False}

        def fake(*args, **kwargs):
            called["value"] = True
            return Message(role="assistant", content="unexpected"), {}

        eng.backend.chat_completion_stream_parsed = fake
        event = threading.Event()
        event.set()
        with _quiet():
            res = eng.submit("fix the parser", cancel_event=event)

        self.assertEqual(res.stop_reason, "cancelled")
        self.assertFalse(called["value"])


if __name__ == "__main__":
    unittest.main()
