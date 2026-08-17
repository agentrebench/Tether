"""Tests for plan mode (feature #5).

Covers two layers:
  - InferenceBackend._build_payload honouring the transient reasoning_effort
    override, including the safety guarantee that sampling-only models are
    never handed a reasoning_effort field.
  - QueryEngine.submit injecting PLAN_MODE_DIRECTIVE and toggling the backend
    override, exercised offline with a stubbed backend (no network, no model).
"""
import io
import contextlib
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from tether.core.config import TetherConfig
from tether.core.permissions import PermissionContext
from tether.core.models import Message
from tether.engine.backend import InferenceBackend
from tether.engine.query_engine import QueryEngine, PLAN_MODE_DIRECTIVE
from tether.tools.base import ToolRegistry


class BuildPayloadReasoningEffort(unittest.TestCase):
    def test_configured_effort_is_used(self):
        cfg = TetherConfig(provider="deepseek", reasoning_effort="high", api_model="x")
        b = InferenceBackend(cfg)
        p = b._build_payload([], None, None, 4096, True)
        self.assertEqual(p.get("reasoning_effort"), "high")
        # reasoning models drop sampling params
        self.assertNotIn("temperature", p)

    def test_override_beats_config(self):
        cfg = TetherConfig(provider="deepseek", reasoning_effort="high", api_model="x")
        b = InferenceBackend(cfg)
        b.reasoning_effort_override = "max"
        p = b._build_payload([], None, None, 4096, True)
        self.assertEqual(p.get("reasoning_effort"), "max")
        self.assertNotIn("temperature", p)

    def test_sampling_model_never_gets_reasoning_effort(self):
        # Local/sampling model: even if an override leaks in, no reasoning_effort
        # field may be emitted (the server would reject it), and temperature stays.
        cfg = TetherConfig(provider="local")
        b = InferenceBackend(cfg)
        b.reasoning_effort_override = ""  # engine sets "" for sampling models
        p = b._build_payload([], None, 0.7, 4096, True)
        self.assertIsNone(p.get("reasoning_effort"))
        self.assertIn("temperature", p)
        self.assertEqual(p["temperature"], 0.7)

    def test_default_no_override_attr(self):
        # _build_payload must not require the attribute to exist.
        cfg = TetherConfig(provider="deepseek", reasoning_effort="high", api_model="x")
        b = InferenceBackend(cfg)
        self.assertFalse(hasattr(b, "reasoning_effort_override"))
        p = b._build_payload([], None, None, 4096, True)
        self.assertEqual(p.get("reasoning_effort"), "high")


def _make_engine(reasoning_effort=""):
    cfg = TetherConfig(provider="local", reasoning_effort=reasoning_effort)
    reg = ToolRegistry.build_default()
    perms = PermissionContext.from_config(cfg)
    return QueryEngine(config=cfg, tool_registry=reg, permissions=perms)


def _stub_backend(engine, recorder):
    """Replace the streaming call with a recorder that captures the messages
    it was handed and returns a no-tool-call assistant message so submit()
    terminates after one round."""
    def fake(messages, tools, temperature=None, **kwargs):
        recorder.append([Message(role=m.role, content=m.content) for m in messages])
        return Message(role="assistant", content="ok"), {}
    engine.backend.chat_completion_stream_parsed = fake
    # Never re-prompt after a plain-text response, so submit returns immediately.
    engine._should_continue_after_text_response = lambda *a, **k: False


class PlanModeSubmit(unittest.TestCase):
    def _run(self, engine):
        recorder = []
        _stub_backend(engine, recorder)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            engine.submit("build me a thing")
        # messages the model saw on the (single) round
        return recorder[0]

    def test_plan_mode_injects_directive(self):
        engine = _make_engine()
        engine.plan_mode = True
        seen = self._run(engine)
        system_msgs = [m.content for m in seen if m.role == "system"]
        user_msgs = [m.content for m in seen if m.role == "user"]
        self.assertTrue(system_msgs, "expected a system message")
        self.assertTrue(user_msgs, "expected a user message")
        self.assertIn(PLAN_MODE_DIRECTIVE, system_msgs[0])
        self.assertEqual(user_msgs[-1], "build me a thing")
        # The directive is inference-only; stored conversation history retains
        # the raw prompt so turning plan mode off cannot leave sticky guidance.
        stored = "\n".join(m.content or "" for m in engine.messages)
        self.assertNotIn("PLAN MODE ENABLED", stored)

    def test_plan_mode_stops_after_presenting_a_plan(self):
        engine = _make_engine()
        engine.plan_mode = True
        calls = []

        def fake(messages, tools, temperature=None, **kwargs):
            calls.append(messages)
            return Message(role="assistant", content="Here is the plan."), {}

        engine.backend.chat_completion_stream_parsed = fake
        # A normal implementation request would trigger the completion guard
        # when no write occurred. Plan mode must stop and wait for approval.
        engine._should_continue_after_text_response = lambda *a, **k: True
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            result = engine.submit("implement the feature")

        self.assertEqual(result.output, "Here is the plan.")
        self.assertEqual(len(calls), 1)

    def test_turning_plan_off_has_no_sticky_directive(self):
        engine = _make_engine()
        first_seen = []
        _stub_backend(engine, first_seen)
        engine.plan_mode = True
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            engine.submit("plan the change")

        engine.plan_mode = False
        second_seen = []
        _stub_backend(engine, second_seen)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            engine.submit("make the change")

        second_prompt = "\n".join(m.content or "" for m in second_seen[0])
        self.assertNotIn("PLAN MODE ENABLED", second_prompt)
        stored_users = [m.content for m in engine.messages if m.role == "user"]
        self.assertIn("plan the change", stored_users)
        self.assertIn("make the change", stored_users)

    def test_plan_mode_off_no_directive(self):
        engine = _make_engine()
        engine.plan_mode = False
        seen = self._run(engine)
        joined = "\n".join(m.content or "" for m in seen)
        self.assertNotIn("PLAN MODE ENABLED", joined)
        self.assertEqual(engine.backend.reasoning_effort_override, "")

    def test_plan_mode_sets_override_for_reasoning_model(self):
        engine = _make_engine(reasoning_effort="high")
        engine.plan_mode = True
        self._run(engine)
        self.assertEqual(engine.backend.reasoning_effort_override, "max")

    def test_plan_mode_no_override_for_sampling_model(self):
        engine = _make_engine(reasoning_effort="")
        engine.plan_mode = True
        self._run(engine)
        self.assertEqual(engine.backend.reasoning_effort_override, "")


if __name__ == "__main__":
    unittest.main()
