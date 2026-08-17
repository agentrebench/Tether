"""Provider catalog and OpenAI-compatible transport regression tests."""
from __future__ import annotations

import os
import json
import sys
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tether.app_bridge import provider_catalog
from tether.cli import config_for_display
from tether.core.config import (
    REMOTE_PROVIDERS,
    TetherConfig,
    apply_provider_selection,
)
from tether.engine.backend import InferenceBackend
from tether.core.models import Message


class ProviderCatalogTests(unittest.TestCase):
    def test_expected_providers_and_fast_deepseek_model_are_available(self):
        self.assertTrue({"deepseek", "kimi", "openai", "glm", "anthropic", "codex"}.issubset(REMOTE_PROVIDERS))
        ids = {model["id"] for model in REMOTE_PROVIDERS["deepseek"]["models"]}
        self.assertEqual(ids, {"deepseek-v4-flash", "deepseek-v4-pro"})

    def test_catalog_is_secret_free_and_reports_keys_per_provider(self):
        config = TetherConfig(
            provider="deepseek",
            api_keys={"deepseek": "deep-secret", "kimi": "kimi-secret"},
        )
        with patch("tether.app_bridge._local_models", return_value=[]):
            catalog = provider_catalog(config)
        represented = repr(catalog)
        self.assertNotIn("deep-secret", represented)
        self.assertNotIn("kimi-secret", represented)
        configured = {item["id"]: item["api_key_configured"] for item in catalog}
        self.assertTrue(configured["deepseek"])
        self.assertTrue(configured["kimi"])

    def test_printable_config_redacts_legacy_and_per_provider_keys(self):
        config = TetherConfig(
            api_key="legacy-secret",
            api_keys={"deepseek": "deep-secret", "kimi": "kimi-secret"},
        )

        displayed = config_for_display(config)

        self.assertEqual(displayed["api_key"], "***")
        self.assertEqual(displayed["api_keys"], {"deepseek": "***", "kimi": "***"})
        self.assertNotIn("secret", repr(displayed))

    def test_switching_provider_does_not_reuse_another_providers_key(self):
        config = TetherConfig(provider="deepseek", api_key="deep-secret")
        apply_provider_selection(config, "kimi", "kimi-k3")
        self.assertEqual(config.stored_api_key(), "")
        self.assertEqual(config.api_keys["deepseek"], "deep-secret")
        config.api_keys["kimi"] = "kimi-secret"
        self.assertEqual(config.stored_api_key(), "kimi-secret")

    def test_legacy_provider_profiles_migrate_on_load(self):
        payload = {
            "default_provider": "kimi",
            "providers": {
                "kimi": {
                    "api_base_url": "https://api.moonshot.ai/v1",
                    "api_model": "kimi-k3",
                    "api_key_env": "KIMI_API_KEY",
                    "api_key": "old-kimi-key",
                },
                "deepseek": {"api_key": "old-deepseek-key"},
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "config.json")
            with open(path, "w") as stream:
                json.dump(payload, stream)
            config = TetherConfig.load(path=Path(path))
        self.assertEqual(config.provider, "kimi")
        self.assertEqual(config.api_model, "kimi-k3")
        self.assertEqual(config.api_key, "")
        self.assertEqual(config.api_keys["kimi"], "old-kimi-key")
        self.assertEqual(config.api_keys["deepseek"], "old-deepseek-key")


class ProviderSelectionTests(unittest.TestCase):
    def test_deepseek_flash_can_disable_thinking_for_speed(self):
        config = TetherConfig()
        apply_provider_selection(
            config,
            "deepseek",
            "deepseek-v4-flash",
            reasoning_effort="off",
        )
        self.assertEqual(config.thinking_mode, "disabled")
        self.assertEqual(config.reasoning_effort, "")
        self.assertFalse(config.omit_sampling)

    def test_kimi_k3_defaults_to_low_effort_with_max_plan_ceiling(self):
        config = TetherConfig()
        apply_provider_selection(config, "kimi", "kimi-k3")
        self.assertEqual(config.reasoning_effort, "low")
        self.assertEqual(config.reasoning_effort_max, "max")
        self.assertEqual(config.max_tokens_field, "max_completion_tokens")

    def test_openai_model_uses_xhigh_for_plan_mode_ceiling(self):
        config = TetherConfig()
        apply_provider_selection(config, "openai", "gpt-5.4-mini")
        self.assertEqual(config.reasoning_effort, "none")
        self.assertEqual(config.reasoning_effort_max, "xhigh")

    def test_unknown_catalog_model_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Unknown glm model"):
            apply_provider_selection(TetherConfig(), "glm", "made-up")


class ProviderTransportTests(unittest.TestCase):
    def test_versioned_v4_base_is_not_mixed_with_v1(self):
        config = TetherConfig(provider="glm", api_base_url="https://api.z.ai/api/paas/v4")
        self.assertEqual(
            InferenceBackend(config).chat_completions_url,
            "https://api.z.ai/api/paas/v4/chat/completions",
        )

    def test_thinking_toggle_is_sent_to_glm(self):
        config = TetherConfig()
        apply_provider_selection(config, "glm", "glm-5.1", thinking_mode="disabled")
        payload = InferenceBackend(config)._build_payload([], None, 0.4, 100, False)
        self.assertEqual(payload["thinking"], {"type": "disabled"})
        self.assertIn("temperature", payload)

    def test_key_header_uses_active_provider_map(self):
        config = TetherConfig(
            provider="kimi",
            api_key_env="TETHER_TEST_MISSING",
            api_keys={"deepseek": "wrong", "kimi": "right"},
        )
        with patch.dict(os.environ, {}, clear=False):
            headers = InferenceBackend(config)._request_headers()
        self.assertEqual(headers["Authorization"], "Bearer right")

    def test_openai_history_drops_provider_specific_reasoning_field(self):
        config = TetherConfig()
        apply_provider_selection(config, "openai", "gpt-5.5")
        message = Message(role="assistant", content="done", reasoning_content="private")
        payload = InferenceBackend(config)._build_payload([message], None, None, 100, False)
        self.assertNotIn("reasoning_content", payload["messages"][0])

    def test_kimi_history_preserves_reasoning_field(self):
        config = TetherConfig()
        apply_provider_selection(config, "kimi", "kimi-k2.7-code")
        message = Message(role="assistant", content="done", reasoning_content="trace")
        payload = InferenceBackend(config)._build_payload([message], None, None, 100, False)
        self.assertEqual(payload["messages"][0]["reasoning_content"], "trace")


if __name__ == "__main__":
    unittest.main()
