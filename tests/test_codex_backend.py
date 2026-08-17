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


if __name__ == "__main__":
    unittest.main()
