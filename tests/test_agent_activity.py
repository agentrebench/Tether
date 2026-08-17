"""Live sub-agent lifecycle, safety, and approval behavior."""
from __future__ import annotations

import contextlib
import io
import json
import threading
import time
import unittest
from unittest.mock import patch

from tether.agents import base as agents_base_module
from tether.agents.base import AgentResult
from tether.core.config import TetherConfig
from tether.core.models import Message, ToolCall, ToolResult, UsageSummary
from tether.core.permissions import APPROVAL_DENIED_SIGNAL, PermissionContext
from tether.tools import agent_tool as agent_tool_module
from tether.tools.agent_tool import AgentTool
from tether.tools.base import BaseTool, ToolRegistry


class _FakeTool(BaseTool):
    def __init__(self, name: str, *, result: ToolResult | None = None):
        self._name = name
        self.result = result or ToolResult(name=name, content="ok")
        self.calls: list[dict] = []

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return "test tool"

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {}}

    def execute(self, arguments: dict) -> ToolResult:
        self.calls.append(arguments)
        return self.result


class _ScriptedBackend:
    def __init__(self, messages: list[Message], *, cancel_event=None):
        self.messages = list(messages)
        self.cancel_event = cancel_event
        self.calls = 0

    def chat_completion(self, **_kwargs):
        self.calls += 1
        if self.cancel_event is not None and self.calls == 1:
            self.cancel_event.set()
        if not self.messages:
            raise AssertionError("backend was called after its script completed")
        message = self.messages.pop(0)
        return message, {"prompt_tokens": 3, "completion_tokens": 2}


class AgentActivityTests(unittest.TestCase):
    def _make_tool(
        self,
        *,
        slots: int = 2,
        registry: ToolRegistry | None = None,
        on_update=None,
        on_approval=None,
    ) -> AgentTool:
        registry = registry or ToolRegistry(tools={})
        tool = AgentTool(
            TetherConfig(parallel_slots=slots),
            registry,
            PermissionContext(),
            on_agent_update=on_update,
            on_approval_request=on_approval,
        )
        self.addCleanup(tool.close)
        return tool

    @staticmethod
    def _execute(tool: AgentTool, arguments: dict, cancel_event=None) -> ToolResult:
        with contextlib.redirect_stdout(io.StringIO()):
            return tool.execute_with_cancel(arguments, cancel_event)

    def test_identities_are_allocated_in_work_item_order_before_parallel_completion(self):
        updates: list[dict] = []
        update_lock = threading.Lock()
        barrier = threading.Barrier(3)
        delays = {"first": 0.06, "second": 0.03, "third": 0.0}

        class FakeSubAgent:
            def __init__(self, *, agent_index, display_label, **_kwargs):
                self.agent_index = agent_index
                self.display_label = display_label
                self.usage = UsageSummary()

            def run(self, task):
                barrier.wait(timeout=1)
                time.sleep(delays[task])
                self.usage = UsageSummary(10 + self.agent_index, self.agent_index)
                return AgentResult(
                    agent_name="explore",
                    agent_index=self.agent_index,
                    display_label=self.display_label,
                    output=f"result for {task}",
                    task=task,
                    usage=self.usage,
                )

        def record(update):
            with update_lock:
                updates.append(update)

        tool = self._make_tool(slots=3, on_update=record)
        with patch.object(agent_tool_module, "SubAgent", FakeSubAgent):
            result = self._execute(tool, {
                "agent_type": "explore",
                "task": "fallback",
                "tasks": ["first", "second", "third"],
            })

        queued = [item for item in updates if item["revision"] == 1]
        self.assertEqual(
            [(item["agent_id"], item["agent_number"], item["task"]) for item in queued],
            [("agent-1", 1, "first"), ("agent-2", 2, "second"), ("agent-3", 3, "third")],
        )
        completed = [item for item in updates if item["status"] == "completed"]
        self.assertEqual([item["agent_number"] for item in completed], [3, 2, 1])
        self.assertLess(result.content.index("agent 1"), result.content.index("agent 2"))
        self.assertLess(result.content.index("agent 2"), result.content.index("agent 3"))

    def test_safe_snapshots_redact_secrets_and_exclude_reasoning_and_raw_content(self):
        updates: list[dict] = []
        bash = _FakeTool("bash")
        registry = ToolRegistry(tools={"bash": bash})
        backend = _ScriptedBackend([
            Message(role="assistant", tool_calls=[ToolCall(
                id="call-secret",
                name="bash",
                arguments={
                    "command": (
                        "API_TOKEN=cmd-secret tool --password hunter2 "
                        "--api-key short-flag-secret curl -H "
                        "'Authorization: Bearer abcdefghijklmnop' example.test"
                    ),
                    "environment": {"PASSWORD": "raw-environment-secret"},
                },
            )]),
            Message(
                role="assistant",
                content=(
                    "Finished. API_KEY=top-secret "
                    + "sk-"
                    + "1234567890abcdef"
                ),
                reasoning_content="PRIVATE_CHAIN_OF_THOUGHT",
            ),
        ])
        tool = self._make_tool(
            slots=1,
            registry=registry,
            on_update=updates.append,
            on_approval=lambda _name, _args: True,
        )

        with patch.object(agents_base_module, "InferenceBackend", return_value=backend):
            result = self._execute(tool, {
                "agent_type": "general",
                "task": "Check the command PASSWORD=hunter2",
            })

        serialized = json.dumps(updates)
        for forbidden in (
            "cmd-secret", "abcdefghijklmnop", "raw-environment-secret",
            "top-secret", "1234567890abcdef", "hunter2", "short-flag-secret",
            "PRIVATE_CHAIN_OF_THOUGHT",
        ):
            self.assertNotIn(forbidden, serialized)
            self.assertNotIn(forbidden, result.content)
        self.assertNotIn("environment", serialized)
        terminal = updates[-1]
        self.assertEqual(terminal["status"], "completed")
        self.assertEqual(terminal["tokens"], 10)
        self.assertGreaterEqual(terminal["elapsed_seconds"], 0)
        self.assertEqual(
            set(terminal),
            {
                "agent_id", "agent_number", "revision", "label", "task",
                "agent_type", "status", "activity", "tool_calls", "output",
                "tokens", "elapsed_seconds",
            },
        )
        self.assertEqual(terminal["tool_calls"][0]["status"], "completed")

    def test_nested_tool_error_has_start_and_terminal_failed_updates(self):
        updates: list[dict] = []
        reader = _FakeTool(
            "file_read",
            result=ToolResult(
                name="file_read",
                content="read failed",
                is_error=True,
                error_code="READ_FAILED",
            ),
        )
        backend = _ScriptedBackend([
            Message(role="assistant", tool_calls=[ToolCall(
                id="read-1", name="file_read", arguments={"file_path": "src/app.py"}
            )]),
            Message(role="assistant", content="Could not read that file."),
        ])
        tool = self._make_tool(
            slots=1,
            registry=ToolRegistry(tools={"file_read": reader}),
            on_update=updates.append,
        )

        with patch.object(agents_base_module, "InferenceBackend", return_value=backend):
            self._execute(tool, {"agent_type": "general", "task": "Check a file"})

        tool_updates = [item for item in updates if item["tool_calls"]]
        self.assertTrue(any(item["tool_calls"][0]["status"] == "running" for item in tool_updates))
        failed = next(
            item for item in tool_updates
            if item["tool_calls"][0]["status"] == "failed"
        )
        self.assertTrue(failed["tool_calls"][0]["is_error"])
        self.assertEqual(failed["tool_calls"][0]["error_code"], "READ_FAILED")

    def test_explicit_child_no_stops_agent_and_propagates_authoritative_code(self):
        updates: list[dict] = []
        cancel_event = threading.Event()
        bash = _FakeTool("bash")
        backend = _ScriptedBackend([
            Message(role="assistant", tool_calls=[ToolCall(
                id="bash-1", name="bash", arguments={"command": "true"}
            )]),
        ])

        def deny(_name, _arguments):
            cancel_event.set()  # Bridge uses this to stop sibling agents.
            return APPROVAL_DENIED_SIGNAL

        tool = self._make_tool(
            slots=1,
            registry=ToolRegistry(tools={"bash": bash}),
            on_update=updates.append,
            on_approval=deny,
        )
        with patch.object(agents_base_module, "InferenceBackend", return_value=backend):
            result = self._execute(
                tool,
                {"agent_type": "general", "task": "Run a command"},
                cancel_event,
            )

        self.assertTrue(result.is_error)
        self.assertEqual(result.error_code, "APPROVAL_DENIED")
        self.assertEqual(updates[-1]["status"], "approval_denied")
        self.assertEqual(updates[-1]["tool_calls"][0]["status"], "denied")
        self.assertEqual(len(bash.calls), 0)
        self.assertEqual(backend.calls, 1)

    def test_feedback_denial_keeps_distinct_error_code(self):
        bash = _FakeTool("bash")
        backend = _ScriptedBackend([
            Message(role="assistant", tool_calls=[ToolCall(
                id="bash-feedback", name="bash", arguments={"command": "true"}
            )]),
        ])
        tool = self._make_tool(
            slots=1,
            registry=ToolRegistry(tools={"bash": bash}),
            on_approval=lambda _name, _args: "use the test runner instead",
        )
        with patch.object(agents_base_module, "InferenceBackend", return_value=backend):
            result = self._execute(tool, {"agent_type": "general", "task": "Run it"})
        self.assertEqual(result.error_code, "APPROVAL_DENIED_WITH_FEEDBACK")
        self.assertEqual(len(bash.calls), 0)

    def test_stop_releasing_child_approval_is_cancelled_not_approval_denied(self):
        updates: list[dict] = []
        cancel_event = threading.Event()
        bash = _FakeTool("bash")
        backend = _ScriptedBackend([
            Message(role="assistant", tool_calls=[ToolCall(
                id="bash-stop", name="bash", arguments={"command": "true"}
            )]),
        ])

        def stop(_name, _arguments):
            cancel_event.set()
            return False

        tool = self._make_tool(
            slots=1,
            registry=ToolRegistry(tools={"bash": bash}),
            on_update=updates.append,
            on_approval=stop,
        )
        with patch.object(agents_base_module, "InferenceBackend", return_value=backend):
            result = self._execute(
                tool,
                {"agent_type": "general", "task": "Run a command"},
                cancel_event,
            )

        self.assertFalse(result.is_error)
        self.assertEqual(result.error_code, "")
        self.assertEqual(updates[-1]["status"], "cancelled")
        self.assertEqual(updates[-1]["tool_calls"][0]["status"], "cancelled")
        self.assertEqual(len(bash.calls), 0)

    def test_cancel_between_model_and_tool_emits_cancelled_terminal_tool_event(self):
        updates: list[dict] = []
        cancel_event = threading.Event()
        reader = _FakeTool("file_read")
        backend = _ScriptedBackend([
            Message(role="assistant", tool_calls=[ToolCall(
                id="read-cancel", name="file_read", arguments={"file_path": "README.md"}
            )]),
        ], cancel_event=cancel_event)
        tool = self._make_tool(
            slots=1,
            registry=ToolRegistry(tools={"file_read": reader}),
            on_update=updates.append,
        )

        with patch.object(agents_base_module, "InferenceBackend", return_value=backend):
            self._execute(
                tool,
                {"agent_type": "general", "task": "Read the README"},
                cancel_event,
            )

        self.assertEqual(updates[-1]["status"], "cancelled")
        self.assertEqual(updates[-1]["tool_calls"][0]["status"], "cancelled")
        self.assertEqual(len(reader.calls), 0)

    def test_child_approval_callbacks_are_serialized(self):
        approval_state = {"active": 0, "maximum": 0, "calls": 0}
        state_lock = threading.Lock()
        bash = _FakeTool("bash")
        registry = ToolRegistry(tools={"bash": bash})

        def backend_factory(_config):
            return _ScriptedBackend([
                Message(role="assistant", tool_calls=[ToolCall(
                    id="bash-shared", name="bash", arguments={"command": "true"}
                )]),
                Message(role="assistant", content="done"),
            ])

        def approve(_name, _arguments):
            with state_lock:
                approval_state["active"] += 1
                approval_state["calls"] += 1
                approval_state["maximum"] = max(
                    approval_state["maximum"], approval_state["active"]
                )
            time.sleep(0.03)
            with state_lock:
                approval_state["active"] -= 1
            return True

        tool = self._make_tool(
            slots=2,
            registry=registry,
            on_approval=approve,
        )
        with patch.object(agents_base_module, "InferenceBackend", side_effect=backend_factory):
            result = self._execute(tool, {
                "agent_type": "general",
                "task": "fallback",
                "tasks": ["task one", "task two"],
            })

        self.assertFalse(result.is_error)
        self.assertEqual(approval_state["calls"], 2)
        self.assertEqual(approval_state["maximum"], 1)
        self.assertEqual(len(bash.calls), 2)

    def test_callback_and_backend_failures_are_isolated_without_traceback(self):
        def broken_callback(_update):
            raise RuntimeError("callback should not escape")

        successful_backend = _ScriptedBackend([Message(role="assistant", content="done")])
        tool = self._make_tool(slots=1, on_update=broken_callback)
        with patch.object(
            agents_base_module, "InferenceBackend", return_value=successful_backend
        ):
            result = self._execute(tool, {"agent_type": "general", "task": "answer"})
        self.assertFalse(result.is_error)

        updates: list[dict] = []

        class FailingBackend:
            def chat_completion(self, **_kwargs):
                raise RuntimeError("API_TOKEN=exception-secret")

        failed_tool = self._make_tool(slots=1, on_update=updates.append)
        with patch.object(
            agents_base_module, "InferenceBackend", return_value=FailingBackend()
        ):
            failed = self._execute(
                failed_tool, {"agent_type": "general", "task": "answer"}
            )
        self.assertTrue(failed.is_error)
        self.assertNotIn("exception-secret", json.dumps(updates))
        self.assertNotIn("Traceback", json.dumps(updates))
        self.assertNotIn("Traceback", failed.content)

    def test_schema_runtime_caps_and_close_registration(self):
        registry = ToolRegistry(tools={})
        tool = self._make_tool(slots=2, registry=registry)
        self.assertEqual(tool.parameters["properties"]["count"]["maximum"], 2)
        self.assertEqual(tool.parameters["properties"]["tasks"]["maxItems"], 2)
        result = self._execute(tool, {
            "agent_type": "general",
            "task": "fallback",
            "tasks": ["one", "two", "three"],
        })
        self.assertTrue(result.is_error)
        self.assertIn("at most 2", result.content)
        self.assertIn(tool.close, registry.close_callbacks)
        registry.close()
        self.assertTrue(tool._closed)
        tool.close()  # Idempotent.


if __name__ == "__main__":
    unittest.main()
