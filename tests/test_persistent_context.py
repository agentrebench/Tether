"""Focused tests for opt-in desktop cross-session context."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from tether.core.config import TetherConfig
from tether.core.models import Message
from tether.core.permissions import PermissionContext
from tether.engine.query_engine import QueryEngine
from tether.tools.base import ToolRegistry


def _make_engine(**kwargs) -> QueryEngine:
    config = TetherConfig(codebase_model_enabled=False)
    return QueryEngine(
        config=config,
        tool_registry=ToolRegistry.build_default(),
        permissions=PermissionContext.from_config(config),
        **kwargs,
    )


class PersistentContextTests(unittest.TestCase):
    def test_desktop_memory_flag_defaults_off_and_round_trips(self):
        self.assertFalse(TetherConfig().desktop_memory_enabled)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            config = TetherConfig(desktop_memory_enabled=True)
            config.save(path)
            self.assertTrue(TetherConfig.load(path).desktop_memory_enabled)

    def test_disabled_context_suppresses_memory_and_last_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            (config_dir / "last_session_summary.txt").write_text("previous session")
            with patch("tether.engine.query_engine.CONFIG_DIR", config_dir), patch(
                "tether.engine.query_engine.load_memory", return_value="persistent notes"
            ) as memory_loader:
                engine = _make_engine(persistent_context_enabled=False)

        system = engine.messages[0].content or ""
        self.assertNotIn("persistent notes", system)
        self.assertNotIn("previous session", system)
        memory_loader.assert_not_called()

    def test_default_cli_context_still_includes_memory_and_last_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            (config_dir / "last_session_summary.txt").write_text("previous session")
            with patch("tether.engine.query_engine.CONFIG_DIR", config_dir), patch(
                "tether.engine.query_engine.load_memory", return_value="persistent notes"
            ):
                engine = _make_engine()

        system = engine.messages[0].content or ""
        self.assertIn("persistent notes", system)
        self.assertIn("previous session", system)

    def test_desktop_can_include_memory_but_omit_global_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            (config_dir / "last_session_summary.txt").write_text("other project")
            with patch("tether.engine.query_engine.CONFIG_DIR", config_dir), patch(
                "tether.engine.query_engine.load_memory", return_value="persistent notes"
            ):
                engine = _make_engine(
                    persistent_context_enabled=True,
                    include_last_session_summary=False,
                )

        system = engine.messages[0].content or ""
        self.assertIn("persistent notes", system)
        self.assertNotIn("other project", system)

    def test_toggle_rebuilds_only_system_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            (config_dir / "last_session_summary.txt").write_text("previous session")
            with patch("tether.engine.query_engine.CONFIG_DIR", config_dir), patch(
                "tether.engine.query_engine.load_memory", return_value="persistent notes"
            ):
                engine = _make_engine(persistent_context_enabled=False)
                user = Message(role="user", content="current prompt")
                assistant = Message(role="assistant", content="current answer")
                engine.messages.extend([user, assistant])

                engine.set_persistent_context_enabled(True)
                self.assertIn("persistent notes", engine.messages[0].content or "")
                self.assertIn("previous session", engine.messages[0].content or "")
                self.assertIs(engine.messages[1], user)
                self.assertIs(engine.messages[2], assistant)

                engine.set_persistent_context_enabled(False)

        system = engine.messages[0].content or ""
        self.assertNotIn("persistent notes", system)
        self.assertNotIn("previous session", system)
        self.assertIs(engine.messages[1], user)
        self.assertIs(engine.messages[2], assistant)


if __name__ == "__main__":
    unittest.main()
