"""Tests for the native-app protocol boundary."""
from __future__ import annotations

import os
import sys
import unittest
import io
import json
import threading
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tether.app_bridge import (
    AppBridgeServer,
    ProtocolWriter,
    _QueuedTurn,
    bridge_status,
    clean_terminal_output,
    normalize_api_key,
)
from tether.cli import build_parser
from tether.core.config import TetherConfig
from tether.core.models import UsageSummary
from tether.core.permissions import APPROVAL_DENIED_SIGNAL
from tether.engine.query_engine import TurnResult


class AppBridgeTests(unittest.TestCase):
    def test_status_is_secret_free(self):
        config = TetherConfig(
            provider="deepseek",
            api_model="deepseek-reasoner",
            api_key_env="TETHER_TEST_KEY",
            api_key="stored-secret",
            desktop_memory_enabled=True,
        )
        with patch.dict(os.environ, {"TETHER_TEST_KEY": "environment-secret"}), \
             patch("tether.app_bridge._local_models", return_value=[]):
            status = bridge_status(config, Path("/tmp/example-project"))

        self.assertEqual(status["provider"], "deepseek")
        self.assertEqual(status["protocol"], 3)
        self.assertEqual(status["model"], "deepseek-reasoner")
        self.assertTrue(status["api_key_configured"])
        self.assertNotIn("stored-secret", repr(status))
        self.assertNotIn("environment-secret", repr(status))
        self.assertGreaterEqual(len(status["providers"]), 8)
        self.assertTrue(status["capabilities"]["clarifying_questions"])
        self.assertTrue(status["capabilities"]["background_jobs"])
        self.assertTrue(status["capabilities"]["slash_commands"])
        self.assertTrue(status["memory_enabled"])
        self.assertFalse(status["plan_mode"])
        self.assertTrue(status["capabilities"]["session_configuration"])

    def test_terminal_cleanup_strips_ansi_and_spinner_frames(self):
        raw = "\x1b[35mread\x1b[0m file.py\n\rThinking...\r\nDone\n"
        self.assertEqual(clean_terminal_output(raw), "read file.py\nDone")

    def test_cli_parser_accepts_internal_bridge_command(self):
        args = build_parser().parse_args(["app-bridge", "--project", "/tmp/project"])
        self.assertEqual(args.command, "app-bridge")
        self.assertEqual(args.project, "/tmp/project")

    def test_api_key_input_rejects_pasted_environment_assignment(self):
        with self.assertRaisesRegex(ValueError, "only the API key value"):
            normalize_api_key("OPENAI_API_KEY=sk-secret")

    def test_api_key_input_accepts_plain_header_value(self):
        self.assertEqual(normalize_api_key("  sk-secret_123  "), "sk-secret_123")

    def test_clarifying_question_round_trip(self):
        output = io.StringIO()
        server = AppBridgeServer.__new__(AppBridgeServer)
        server.writer = ProtocolWriter(output)
        server._pending = {}
        server._pending_questions = {}
        server._pending_lock = threading.Lock()
        server._cancel_event = None
        server._shutdown = threading.Event()
        received: list[list[dict]] = []

        worker = threading.Thread(
            target=lambda: received.append(server._ask_clarifying_questions([
                {"question": "Which UI?", "options": ["Web", "Native"]}
            ]))
        )
        worker.start()
        while not output.getvalue():
            worker.join(0.01)
        request = json.loads(output.getvalue().splitlines()[0])
        server._resolve_questions({
            "request_id": request["request_id"],
            "answers": [{"question": "Which UI?", "answer": "Web"}],
        })
        worker.join(1)

        self.assertEqual(received[0][0]["answer"], "Web")

    def test_plan_command_toggles_engine_mode_and_returns_gui_result(self):
        output = io.StringIO()
        server = AppBridgeServer.__new__(AppBridgeServer)
        server.writer = ProtocolWriter(output)
        server._state_lock = threading.Lock()
        server._busy = False
        server.project = Path("/tmp/example-project")
        server.config = SimpleNamespace(provider="deepseek", api_model="deepseek-chat")
        server.engine = SimpleNamespace(
            plan_mode=False,
            usage=SimpleNamespace(input_tokens=0, output_tokens=0, total=0),
            get_context_usage_ratio=lambda: 0.25,
        )

        server._run_command({"id": "command-1", "command": "/plan on"})

        result = json.loads(output.getvalue())
        self.assertTrue(result["ok"])
        self.assertTrue(result["plan_mode"])
        self.assertTrue(server.engine.plan_mode)
        self.assertIn("Plan mode is **on**", result["message"])

    def test_session_settings_update_engine_and_return_authoritative_state(self):
        output = io.StringIO()
        server = AppBridgeServer.__new__(AppBridgeServer)
        server.writer = ProtocolWriter(output)
        server._state_lock = threading.Lock()
        server._busy = False

        saved: list[bool] = []
        memory_updates: list[bool] = []
        todo_saves: list[list[dict]] = []
        server.config = SimpleNamespace(
            desktop_memory_enabled=False,
            save=lambda: saved.append(server.config.desktop_memory_enabled),
        )
        server.engine = SimpleNamespace(
            plan_mode=False,
            set_persistent_context_enabled=memory_updates.append,
        )
        server.todo_store = SimpleNamespace(save=lambda todos: todo_saves.append(todos))
        server.todo_tool = SimpleNamespace(
            store=None,
            snapshot=lambda: [{"content": "Keep current work", "status": "pending"}],
        )
        server._status = lambda: {
            "type": "hello",
            "memory_enabled": server.config.desktop_memory_enabled,
            "plan_mode": server.engine.plan_mode,
        }

        server._configure_session({
            "memory_enabled": True,
            "plan_mode": True,
        })

        result = json.loads(output.getvalue())
        self.assertEqual(result["type"], "session_configured")
        self.assertTrue(result["memory_enabled"])
        self.assertTrue(result["plan_mode"])
        self.assertEqual(saved, [True])
        self.assertEqual(memory_updates, [True])
        self.assertIs(server.todo_tool.store, server.todo_store)
        self.assertEqual(todo_saves[0][0]["content"], "Keep current work")

    def test_session_settings_reject_changes_during_active_turn(self):
        output = io.StringIO()
        server = AppBridgeServer.__new__(AppBridgeServer)
        server.writer = ProtocolWriter(output)
        server._state_lock = threading.Lock()
        server._busy = True

        server._configure_session({"plan_mode": True})

        result = json.loads(output.getvalue())
        self.assertEqual(result["type"], "error")
        self.assertIn("active turn", result["message"])

    def test_gui_command_catalog_matches_terminal_commands_and_skills(self):
        server = AppBridgeServer.__new__(AppBridgeServer)
        server.engine = SimpleNamespace(
            tools=SimpleNamespace(names=lambda: ["bash", "file_read", "file_edit"])
        )

        catalog = {item["command"] for item in server._command_catalog()}

        self.assertIn("/plan", catalog)
        self.assertIn("/persistence build", catalog)
        self.assertIn("/memory show", catalog)
        self.assertIn("/systematic-debugging", catalog)
        self.assertNotIn("/research", catalog)
        self.assertNotIn("/mental-model", catalog)

    def test_why_command_starts_an_agent_turn(self):
        server = AppBridgeServer.__new__(AppBridgeServer)
        server.writer = ProtocolWriter(io.StringIO())
        server._state_lock = threading.Lock()
        server._busy = False
        captured = []
        server._start_submit = captured.append

        server._run_command({"id": "why-1", "command": "/why desktop/src/App.tsx"})

        self.assertEqual(captured[0]["id"], "why-1")
        self.assertIn("Explain why desktop/src/App.tsx", captured[0]["prompt"])

    def test_persistence_command_runs_off_the_bridge_input_thread(self):
        output = io.StringIO()
        server = AppBridgeServer.__new__(AppBridgeServer)
        server.writer = ProtocolWriter(output)
        server._state_lock = threading.Lock()
        server._busy = False
        server._cancel_event = None
        server.engine = SimpleNamespace(plan_mode=False)
        started = threading.Event()
        release = threading.Event()

        def persistence_command(argument, on_progress=None):
            started.set()
            release.wait(1)
            return True, "Mental model ready."

        server._persistence_command = persistence_command
        server._run_command({"id": "persistence-1", "command": "/persistence sync"})

        self.assertTrue(started.wait(0.5))
        self.assertTrue(server._busy)
        progress = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(progress[0]["type"], "command_progress")

        release.set()
        for _ in range(100):
            if not server._busy:
                break
            threading.Event().wait(0.01)
        self.assertFalse(server._busy)
        events = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(events[-1]["type"], "command_result")
        self.assertEqual(events[-1]["message"], "Mental model ready.")

    def test_followup_prompts_queue_and_run_in_fifo_order(self):
        output = io.StringIO()
        first_started = threading.Event()
        release_first = threading.Event()

        class FakeEngine:
            def __init__(self):
                self.usage = UsageSummary()
                self.prompts: list[str] = []

            def submit(self, prompt, cancel_event=None):
                self.prompts.append(prompt)
                if len(self.prompts) == 1:
                    first_started.set()
                    release_first.wait(1)
                self.usage = self.usage.add_turn(2, 3)
                return TurnResult(output=f"done: {prompt}", usage=self.usage)

        server = AppBridgeServer.__new__(AppBridgeServer)
        server.writer = ProtocolWriter(output)
        server.engine = FakeEngine()
        server._state_lock = threading.Lock()
        server._pending_lock = threading.Lock()
        server._pending = {}
        server._pending_questions = {}
        server._shutdown = threading.Event()
        server._queued_turns = deque()
        server._busy = False
        server._cancel_event = None
        server._active_turn_id = ""
        server._last_stream_phase = ""

        server._start_submit({"id": "turn-1", "prompt": "first"})
        self.assertTrue(first_started.wait(0.5))
        server._start_submit({"id": "turn-2", "prompt": "second"})
        release_first.set()

        for _ in range(100):
            events = [json.loads(line) for line in output.getvalue().splitlines()]
            if any(event["type"] == "bridge_idle" for event in events):
                break
            threading.Event().wait(0.01)

        self.assertEqual(server.engine.prompts, ["first", "second"])
        events = [json.loads(line) for line in output.getvalue().splitlines()]
        event_keys = [
            (event["type"], event.get("id"))
            for event in events
        ]
        self.assertIn(("turn_queued", "turn-2"), event_keys)
        self.assertLess(
            event_keys.index(("turn_completed", "turn-1")),
            event_keys.index(("turn_started", "turn-2")),
        )
        self.assertEqual(events[-1]["type"], "bridge_idle")
        self.assertFalse(server._busy)

    def test_approval_response_is_consumed_once(self):
        output = io.StringIO()
        server = AppBridgeServer.__new__(AppBridgeServer)
        server.writer = ProtocolWriter(output)
        server._state_lock = threading.Lock()
        server._pending_lock = threading.Lock()
        server._pending = {}
        server._pending_questions = {}
        server._shutdown = threading.Event()
        server._cancel_event = None
        server._queued_turns = deque()
        received: list[bool | str] = []

        worker = threading.Thread(
            target=lambda: received.append(server._request_approval("bash", {"command": "pwd"}))
        )
        worker.start()
        while not output.getvalue():
            worker.join(0.01)
        request = json.loads(output.getvalue().splitlines()[0])
        response = {
            "request_id": request["request_id"],
            "decision": "allow_once",
        }
        server._resolve_approval(response)
        server._resolve_approval(response)
        worker.join(1)

        self.assertEqual(received, [True])
        events = [json.loads(line) for line in output.getvalue().splitlines()]
        resolved = [event for event in events if event["type"] == "approval_resolved"]
        self.assertEqual([event["accepted"] for event in resolved], [True, False])
        self.assertFalse(resolved[0]["stale"])
        self.assertTrue(resolved[1]["stale"])

    def test_denial_discards_prior_followups_before_waking_engine(self):
        output = io.StringIO()
        server = AppBridgeServer.__new__(AppBridgeServer)
        server.writer = ProtocolWriter(output)
        server._state_lock = threading.Lock()
        server._pending_lock = threading.Lock()
        server._pending = {}
        server._pending_questions = {}
        server._shutdown = threading.Event()
        server._cancel_event = threading.Event()
        server._queued_turns = deque([
            _QueuedTurn("turn-2", "old direction"),
            _QueuedTurn("turn-3", "another old direction"),
        ])
        received: list[bool | str] = []

        worker = threading.Thread(
            target=lambda: received.append(server._request_approval("bash", {"command": "pwd"}))
        )
        worker.start()
        while not output.getvalue():
            worker.join(0.01)
        request = json.loads(output.getvalue().splitlines()[0])
        server._resolve_approval({
            "request_id": request["request_id"],
            "decision": "deny",
        })
        worker.join(1)

        self.assertEqual(received, [APPROVAL_DENIED_SIGNAL])
        self.assertTrue(server._cancel_event.is_set())
        self.assertEqual(list(server._queued_turns), [])
        events = [json.loads(line) for line in output.getvalue().splitlines()]
        cleared = next(event for event in events if event["type"] == "turn_queue_cleared")
        self.assertEqual(cleared["ids"], ["turn-2", "turn-3"])
        self.assertEqual(cleared["reason"], "approval_denied")

    def test_agent_updates_are_scoped_to_active_parent_turn(self):
        output = io.StringIO()
        server = AppBridgeServer.__new__(AppBridgeServer)
        server.writer = ProtocolWriter(output)
        server._state_lock = threading.Lock()
        server._active_turn_id = "parent-7"

        server._forward_agent_update({
            "agent_id": "agent-2",
            "agent_number": 2,
            "revision": 1,
            "status": "running",
        })
        event = json.loads(output.getvalue())
        self.assertEqual(event["type"], "agent_updated")
        self.assertEqual(event["id"], "parent-7")
        self.assertEqual(event["agent_id"], "agent-2")

        server._active_turn_id = ""
        server._forward_agent_update({"agent_id": "agent-3", "revision": 1})
        self.assertEqual(len(output.getvalue().splitlines()), 1)

    def test_approval_denial_returns_direction_prompt_before_idle(self):
        output = io.StringIO()

        class DeniedEngine:
            usage = UsageSummary()

            def submit(self, prompt, cancel_event=None):
                return TurnResult(
                    output="I didn’t run that command. What would you like me to do instead?",
                    usage=self.usage,
                    stop_reason="approval_denied",
                )

        server = AppBridgeServer.__new__(AppBridgeServer)
        server.writer = ProtocolWriter(output)
        server.engine = DeniedEngine()
        server._state_lock = threading.Lock()
        server._pending_lock = threading.Lock()
        server._pending = {}
        server._pending_questions = {}
        server._shutdown = threading.Event()
        server._queued_turns = deque()
        server._busy = False
        server._cancel_event = None
        server._active_turn_id = ""
        server._last_stream_phase = ""

        server._start_submit({"id": "turn-denied", "prompt": "run it"})
        for _ in range(100):
            events = [json.loads(line) for line in output.getvalue().splitlines()]
            if any(event["type"] == "bridge_idle" for event in events):
                break
            threading.Event().wait(0.01)

        events = [json.loads(line) for line in output.getvalue().splitlines()]
        event_types = [event["type"] for event in events]
        self.assertEqual(
            event_types[-3:],
            ["turn_completed", "direction_required", "bridge_idle"],
        )
        self.assertIn("What should Tether do instead?", events[-2]["message"])


if __name__ == "__main__":
    unittest.main()
