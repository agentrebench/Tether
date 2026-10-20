"""Safety regressions for the optional Codex CLI backend."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tether.core.config import TetherConfig
from tether.core.models import Message
from tether.engine.codex_backend import CodexExecBackend


class CodexBackendSafetyTests(unittest.TestCase):
    def test_nested_codex_defaults_to_read_only_without_config_access(self):
        with tempfile.TemporaryDirectory() as directory:
            backend = CodexExecBackend(
                TetherConfig(provider="codex", api_model="gpt-5.5"),
                cwd=directory,
            )
            with patch.dict(
                os.environ,
                {
                    "TETHER_CODEX_SANDBOX": "",
                    "TETHER_CODEX_APPROVAL": "",
                },
                clear=False,
            ):
                # Empty values are removed so this exercises the secure defaults.
                os.environ.pop("TETHER_CODEX_SANDBOX", None)
                os.environ.pop("TETHER_CODEX_APPROVAL", None)
                _, command, _, output = backend._build_exec_cmd("codex", json_mode=True)

        self.assertIsNone(output)
        self.assertEqual(command[command.index("-s") + 1], "read-only")
        self.assertEqual(command[command.index("-a") + 1], "never")
        self.assertNotIn("--add-dir", command)
        self.assertNotIn(str(Path.home() / ".tether"), command)

    def test_prompt_delegates_tools_and_approvals_to_outer_engine(self):
        prompt = CodexExecBackend._build_prompt(
            [Message(role="user", content="Explain this function.")]
        )

        self.assertIn("outer Tether engine owns tools", prompt)
        self.assertIn("Explain this function.", prompt)
        self.assertNotIn("Marq", prompt)

    def test_context_override_reaches_codex_command(self):
        for size in (1_000_000, 123456):
            backend = CodexExecBackend(TetherConfig(provider="codex", context_size=size))
            _, command, _, _ = backend._build_exec_cmd("codex", json_mode=True)
            self.assertIn(f"model_context_window={size}", command)

    def test_long_messages_and_history_are_not_silently_cut_at_old_caps(self):
        content = "BEGIN " + "x" * 50000 + " MIDDLE " + "y" * 70000 + " END"
        with patch.dict(os.environ, {}, clear=True):
            prompt = CodexExecBackend._build_prompt([Message(role="user", content=content)])
        self.assertIn(content, prompt)
        self.assertNotIn("truncated", prompt)

    def test_serialized_prompt_remains_bounded_by_context_and_explicit_cap(self):
        messages = [Message(role="user", content="x" * 100000 + "LATEST")]
        for cap, expected in (("5000", 5000), ("999999", 40000)):
            with patch.dict(os.environ, {"TETHER_CODEX_PROMPT_CHARS": cap}):
                prompt = CodexExecBackend._build_prompt(messages, context_size=10000)
            self.assertLessEqual(len(prompt), expected)
            self.assertIn("outer Tether engine owns tools", prompt)
            self.assertIn("Earlier conversation truncated", prompt)
            self.assertTrue(prompt.endswith("LATEST"))


if __name__ == "__main__":
    unittest.main()
