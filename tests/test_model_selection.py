"""Provider-scoped discovery, selection, request routing, and terminal UI."""
import asyncio
import io
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from prompt_toolkit.application import Application
from prompt_toolkit.application.current import set_app
from prompt_toolkit.document import Document
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.layout import Layout

from tether.core import config as catalog
from tether.core.config import TetherConfig, apply_provider_selection, provider_models
from tether.engine.codex_backend import CodexExecBackend
from tether.engine.backend import InferenceBackend
from tether.ui.agent_picker import agent_choices
from tether.ui.model_dialog import ModelDialog
from tether.ui.repl import TetherCompleter, TetherREPL
from tether.ui.workspace import TerminalWorkspace
from .test_terminal_workspace import PaintedOutput, make_repl


@pytest.fixture(autouse=True)
def isolated_catalog(monkeypatch, tmp_path):
    monkeypatch.setattr(catalog, "_discovered_models", {})
    monkeypatch.setattr(catalog, "_codex_models_cache", {})
    path = tmp_path / "models_cache.json"
    path.write_text(json.dumps({"models": [
        {"slug": "gpt-6.1-sol", "display_name": "GPT-6.1-Sol", "priority": 1},
        {"slug": "gpt-6-astra", "display_name": "GPT-6-Astra", "priority": 2,
         "context_window": 272000, "default_reasoning_level": "medium",
         "supported_reasoning_levels": [{"effort": "low"}, {"effort": "medium"}, {"effort": "ultra"}]},
        {"slug": "gpt-5.5", "visibility": "hide", "priority": 3},
    ]}))
    monkeypatch.setattr(catalog, "CODEX_MODELS_CACHE", path)
    for preset in catalog.REMOTE_PROVIDERS.values():
        for key in [preset.get("api_key_env", ""), *preset.get("api_key_env_aliases", [])]:
            if key:
                monkeypatch.delenv(key, raising=False)


def repl_for(config):
    repl = TetherREPL.__new__(TetherREPL)
    repl.config = config
    repl._workspace = None
    config.save = Mock()
    return repl


def response(ids, **kwargs):
    return io.BytesIO(json.dumps({"data": [{"id": i} for i in ids], **kwargs}).encode())


def test_codex_menu_default_and_picker_follow_cli_catalog():
    config = TetherConfig(provider="anthropic", api_model="claude-saved")
    choice = next(c for c in agent_choices(config) if c.provider == "codex")
    assert choice.model == "gpt-6.1-sol"
    apply_provider_selection(config, "codex")
    assert config.api_model == choice.model
    assert [m["id"] for m in provider_models("codex", config)] == ["gpt-6.1-sol", "gpt-6-astra"]


def test_bare_model_lists_codex_and_number_selection_reaches_backend(capsys):
    config = TetherConfig(provider="codex", api_model="gpt-6.1-sol")
    repl = repl_for(config)
    repl._handle_model(["/model"], SimpleNamespace())
    output = capsys.readouterr().out
    assert "gpt-6-astra" in output and "claude" not in output
    repl._handle_model(["/model", "2"], SimpleNamespace())
    assert config.provider == "codex" and config.api_model == "gpt-6-astra"
    assert config.reasoning_effort == "medium" and config.reasoning_effort_max == "ultra"
    assert config.context_size == 272000
    config.save.assert_called_once()
    _, command, _, _ = CodexExecBackend(config)._build_exec_cmd("codex", json_mode=True)
    assert command[command.index("-m") + 1] == "gpt-6-astra"


def test_direct_selection_stays_in_provider_and_invalid_selection_preserves_model(capsys):
    config = TetherConfig(provider="codex", api_model="gpt-6.1-sol")
    repl = repl_for(config)
    repl._handle_model(["/model", "gpt-6-astra"], None)
    repl._handle_model(["/model", "claude-not-a-codex-model"], None)
    assert (config.provider, config.api_model) == ("codex", "gpt-6-astra")
    assert "Unknown codex model" in capsys.readouterr().out


def test_numbers_cannot_select_from_previous_provider(capsys):
    config = TetherConfig(provider="codex", api_model="gpt-6.1-sol")
    repl = repl_for(config)
    repl._handle_model(["/model"], None)
    apply_provider_selection(config, "anthropic")
    saved_model = config.api_model
    repl._handle_model(["/model", "2"], None)
    assert config.api_model == saved_model
    config.save.assert_not_called()
    assert "Run /model" in capsys.readouterr().out


def test_api_models_include_all_pages_and_only_this_accounts_models(monkeypatch):
    config = TetherConfig(provider="anthropic", api_keys={"anthropic": "test"})
    get = Mock(side_effect=[response(["claude-new"], has_more=True, last_id="claude-new"),
                            response(["claude-other"], has_more=False)])
    monkeypatch.setattr(catalog.urllib.request, "urlopen", get)
    assert {m["id"] for m in provider_models("anthropic", config)} == {"claude-new", "claude-other"}
    assert get.call_args.args[0].full_url.endswith("?after_id=claude-new")
    apply_provider_selection(config, "anthropic", "claude-new")
    assert config.api_model == "claude-new"
    assert get.call_count == 2


def test_refresh_and_account_changes_do_not_reuse_the_wrong_catalog(monkeypatch):
    config = TetherConfig(api_keys={"glm": "first"})
    get = Mock(side_effect=[response(["glm-first"]), response(["glm-fresh"]), response(["glm-second"])])
    monkeypatch.setattr(catalog.urllib.request, "urlopen", get)
    assert provider_models("glm", config)[0]["id"] == "glm-first"
    assert provider_models("glm", config, force=True)[0]["id"] == "glm-fresh"
    config.api_keys["glm"] = "second"
    assert provider_models("glm", config)[0]["id"] == "glm-second"


def test_custom_endpoint_models_are_discovered_and_selectable(monkeypatch):
    config = TetherConfig(provider="custom", api_base_url="http://localhost:8081/v1", api_model="old")
    get = Mock(return_value=response(["Model/CaseSensitive"]))
    monkeypatch.setattr(catalog.urllib.request, "urlopen", get)
    repl = repl_for(config)
    repl._handle_model(["/model", "list"], None)
    assert get.call_args.args[0].full_url == "http://localhost:8081/v1/models"
    repl._handle_model(["/model", "1"], None)
    assert config.api_model == "Model/CaseSensitive"
    assert config.api_base_url == "http://localhost:8081/v1"


def test_completion_follows_active_provider_without_network(monkeypatch):
    get = Mock(side_effect=AssertionError("Typing must not request models"))
    monkeypatch.setattr(catalog.urllib.request, "urlopen", get)
    config = TetherConfig(provider="codex")
    completer = TetherCompleter(config=config)
    def entries():
        return [c.text for c in completer.get_completions(Document("/model "), None)]
    assert "/model gpt-6-astra" in entries()
    config.provider = "anthropic"
    assert "/model gpt-6-astra" not in entries()
    assert any("claude" in item for item in entries())
    get.assert_not_called()


def test_model_dialog_renders_filters_and_selects_astra():
    async def exercise():
        with create_pipe_input() as pipe:
            models = provider_models("codex", TetherConfig())
            output = PaintedOutput()
            dialog = ModelDialog(models, "Codex", "gpt-6.1-sol", "Codex CLI catalog",
                                 lambda value: app.exit(result=value), lambda: app.exit())
            app = Application(layout=Layout(dialog.dialog, focused_element=dialog.search),
                              input=pipe, output=output)
            with set_app(app):
                app.renderer.render(app, app.layout)
                screen = app.renderer.last_rendered_screen
                rendered = "\n".join("".join(cell.char for _, cell in sorted(row.items()))
                                     for _, row in sorted(screen.data_buffer.items()))
                assert "GPT-6-Astra" in rendered and "Codex" in rendered
                await app.cancel_and_wait_for_background_tasks()
            running = asyncio.create_task(app.run_async())
            await asyncio.sleep(.05)
            pipe.send_text("astra\r")
            assert await asyncio.wait_for(running, 2) == "gpt-6-astra"
    asyncio.run(exercise())


def test_workspace_picker_queues_model_change_and_escape_preserves_selection():
    async def exercise():
        with create_pipe_input() as pipe:
            repl = make_repl()
            repl.config.provider = "codex"
            repl.config.api_model = "gpt-6.1-sol"
            ui = TerminalWorkspace(repl, input=pipe, output=PaintedOutput())
            running = asyncio.create_task(ui.app.run_async())
            try:
                await asyncio.sleep(.05)
                ui.open_models(provider_models("codex", repl.config), "Codex", "Codex CLI catalog")
                await asyncio.sleep(.05)
                pipe.send_text("\x1b")
                await asyncio.sleep(.6)
                assert ui.dialog is None and ui.inbox.empty()
                ui.open_models(provider_models("codex", repl.config), "Codex", "Codex CLI catalog")
                await asyncio.sleep(.05)
                pipe.send_text("\x1b[B\r")
                await asyncio.sleep(.1)
                assert ui.inbox.get_nowait() == "/model gpt-6-astra"
                assert repl.config.api_model == "gpt-6.1-sol"
            finally:
                ui.app.exit()
                await running
    asyncio.run(exercise())


def test_effort_selection_persists_and_reaches_codex_without_changing_model(monkeypatch, tmp_path, capsys):
    config_file = tmp_path / "tether-config.json"
    monkeypatch.setattr(catalog, "CONFIG_FILE", config_file)
    config = TetherConfig()
    apply_provider_selection(config, "codex", "gpt-6-astra")
    config.context_size = 123456
    config.max_budget_tokens = 987654
    repl = TetherREPL.__new__(TetherREPL)
    repl.config, repl._workspace = config, None
    assert repl._handle_slash_command("/effort ultra", None)
    assert (config.provider, config.api_model, config.reasoning_effort) == ("codex", "gpt-6-astra", "ultra")
    assert (config.context_size, config.max_budget_tokens) == (123456, 987654)
    loaded = TetherConfig.load(config_file)
    assert loaded.reasoning_effort == "ultra"
    _, command, _, _ = CodexExecBackend(loaded)._build_exec_cmd("codex", json_mode=True)
    assert 'model_reasoning_effort="ultra"' in command
    repl._handle_slash_command("/model stats", SimpleNamespace())
    assert "effort:   ultra" in capsys.readouterr().out


@pytest.mark.parametrize("provider,model,setting,expected", [
    ("openai", "gpt-5.4-mini", "high", {"reasoning_effort": "high"}),
    ("openai", "gpt-5.4-mini", "none", {"reasoning_effort": "none"}),
    ("deepseek", "deepseek-v4-flash", "off", {"thinking": {"type": "disabled"}}),
    ("deepseek", "deepseek-v4-flash", "max", {"reasoning_effort": "max", "thinking": {"type": "enabled"}}),
    ("glm", "glm-5.2", "disabled", {"thinking": {"type": "disabled"}}),
    ("kimi", "kimi-k3", "max", {"reasoning_effort": "max"}),
])
def test_effort_uses_provider_specific_request_fields(provider, model, setting, expected):
    config = TetherConfig()
    apply_provider_selection(config, provider, model)
    repl = repl_for(config)
    repl._handle_effort(["/effort", setting], None)
    payload = InferenceBackend(config)._build_payload([], None, .4, 100, False)
    for key, value in expected.items():
        assert payload[key] == value
    if setting in ("off", "disabled"):
        assert "reasoning_effort" not in payload
    assert catalog.reasoning_setting(config)[2] == setting
    assert config.api_model == model and config.provider == provider
    config.save.assert_called_once()


def test_effort_list_marks_current_and_rejects_invalid_or_stale_number(capsys):
    config = TetherConfig()
    apply_provider_selection(config, "codex", "gpt-6-astra")
    repl = repl_for(config)
    repl._handle_effort(["/effort"], None)
    assert "2. medium *" in capsys.readouterr().out
    repl._handle_effort(["/effort", "unsupported"], None)
    assert config.reasoning_effort == "medium"
    assert "Unsupported reasoning setting" in capsys.readouterr().out
    repl._handle_effort(["/effort", "3"], None)
    assert config.reasoning_effort == "ultra"
    apply_provider_selection(config, "deepseek", "deepseek-v4-flash")
    repl._handle_effort(["/effort", "3"], None)
    assert config.thinking_mode == "disabled"
    assert "Run /effort" in capsys.readouterr().out


@pytest.mark.parametrize("config", [TetherConfig(), TetherConfig(provider="anthropic", api_model="claude-opus-5")])
def test_unsupported_models_do_not_get_invented_effort_levels(config, capsys):
    repl = repl_for(config)
    repl._handle_effort(["/effort", "ultra"], None)
    assert "No configurable reasoning levels" in capsys.readouterr().out
    config.save.assert_not_called()


def test_effort_completion_updates_with_model_without_network(monkeypatch):
    get = Mock(side_effect=AssertionError("Typing must not make requests"))
    monkeypatch.setattr(catalog.urllib.request, "urlopen", get)
    config = TetherConfig()
    apply_provider_selection(config, "codex", "gpt-6-astra")
    completer = TetherCompleter(config=config)
    def entries():
        return [c.text for c in completer.get_completions(Document("/effort "), None)]
    assert "/effort ultra" in entries() and "/effort off" not in entries()
    apply_provider_selection(config, "deepseek", "deepseek-v4-flash")
    assert "/effort off" in entries() and "/effort ultra" not in entries()
    get.assert_not_called()


@pytest.mark.parametrize("rows,columns", [(36, 120), (24, 80), (20, 60)])
def test_effort_picker_renders_current_setting_and_selects_with_keyboard(rows, columns):
    async def exercise():
        with create_pipe_input() as pipe:
            repl = make_repl()
            apply_provider_selection(repl.config, "codex", "gpt-6-astra")
            output = PaintedOutput()
            output.size = Size(rows=rows, columns=columns)
            ui = TerminalWorkspace(repl, input=pipe, output=output)
            running = asyncio.create_task(ui.app.run_async())
            try:
                await asyncio.sleep(.05)
                ui.open_effort(["low", "medium", "ultra"], "medium", "gpt-6-astra · current: medium")
                await asyncio.sleep(.1)
                screen = ui.app.renderer.last_rendered_screen
                rendered = "\n".join("".join(screen.data_buffer[y][x].char for x in range(columns))
                                     for y in range(rows))
                assert "Choose reasoning effort" in rendered
                assert "* medium" in rendered and "ultra" in rendered
                assert "<  Select  >" in rendered
                assert "effort medium" in ui._header()
                pipe.send_text("\x1b[B\r")
                await asyncio.sleep(.1)
                assert ui.inbox.get_nowait() == "/effort ultra"
                assert ui.dialog is None and repl.config.reasoning_effort == "medium"
            finally:
                ui.app.exit()
                await running
    asyncio.run(exercise())
