"""Local model selection by number or name, at startup and from /model."""
import builtins
from unittest.mock import Mock

from tether import cli
from tether.cli import DiscoveredModel, resolve_model_choice, select_model
from tether.core.config import TetherConfig
from tether.ui.repl import TetherREPL


def models(tmp_path, context=None):
    found = []
    for name, file in [("google/gemma-4-31B-it", "gemma-4-31b-it-Q4_K_M.gguf"),
                       ("Qwen3-Coder-30B", "Qwen3-Coder-30B-Q8_0.gguf"), ("Qwen3-8B", "Qwen3-8B-Q4.gguf")]:
        path = tmp_path / file
        path.write_bytes(b"GGUF")
        found.append(DiscoveredModel(name=name, path=path, format="gguf", size_bytes=1 << 30,
                                     source="local", context_length=context))
    return found


def local_repl(found, workspace=None):
    config = TetherConfig(provider="local", model_path=str(found[0].path))
    config.save = Mock()
    repl = TetherREPL.__new__(TetherREPL)
    repl.config = config
    repl._workspace = workspace
    return repl, config


def test_resolve_by_number_name_file_path_and_unique_fragment(tmp_path):
    found = models(tmp_path)
    assert resolve_model_choice("2", found)[0] is found[1]
    assert resolve_model_choice("google/gemma-4-31B-it", found)[0] is found[0]
    assert resolve_model_choice("GEMMA", found)[0] is found[0]
    assert resolve_model_choice("Qwen3-8B-Q4.gguf", found)[0] is found[2]
    assert resolve_model_choice(str(found[2].path), found)[0] is found[2]
    assert resolve_model_choice("qwen3-8b", found)[0] is found[2]
    model, problem = resolve_model_choice("qwen", found)
    assert model is None and "several" in problem and "Qwen3-Coder-30B" in problem
    assert resolve_model_choice("9", found) == (None, "Invalid choice: 9 (choose 1-3)")
    assert resolve_model_choice("llama", found)[0] is None
    assert resolve_model_choice("", found)[0] is None


def test_startup_selector_accepts_a_model_name(monkeypatch, tmp_path, capsys):
    found = models(tmp_path)
    monkeypatch.setattr(cli, "discover_models", lambda extra_dirs=None: found)
    monkeypatch.setattr(builtins, "input", lambda prompt="": "coder")
    config = TetherConfig(provider="local", model_path="")
    config.save = Mock()
    assert select_model(config) == found[1].path
    assert "Enter a number or a model name" in capsys.readouterr().out
    monkeypatch.setattr(builtins, "input", lambda prompt="": "qwen")
    assert select_model(config) is None
    assert "matches several models" in capsys.readouterr().out


def test_model_lists_local_models_numbered_and_switches_by_number(monkeypatch, tmp_path, capsys):
    found = models(tmp_path, context=32768)
    repl, config = local_repl(found)
    monkeypatch.setattr(cli, "discover_models", lambda extra_dirs=None: found)
    monkeypatch.setattr(cli, "served_model_path", lambda cfg: str(found[0].path))
    stop, launch = Mock(return_value=True), Mock(return_value=object())
    monkeypatch.setattr(cli, "stop_server_on_port", stop)
    monkeypatch.setattr(cli, "_launch_server_background", launch)
    repl._handle_model(["/model"], None)
    out = capsys.readouterr().out
    assert "1. google/gemma-4-31B-it" in out and "2. Qwen3-Coder-30B" in out
    assert "ctx 32k" in out and "*" in out and "/model <number or model name>" in out
    repl._handle_model(["/model", "2"], None)
    stop.assert_called_once_with(config)
    launch.assert_called_once_with(config, model_override=found[1].path)
    assert config.model_path == str(found[1].path)
    config.save.assert_called_once()
    assert "switched to local (Qwen3-Coder-30B)" in capsys.readouterr().out


def test_model_by_name_skips_restart_when_already_served_and_rejects_ambiguity(monkeypatch, tmp_path, capsys):
    found = models(tmp_path)
    repl, config = local_repl(found)
    monkeypatch.setattr(cli, "discover_models", lambda extra_dirs=None: found)
    monkeypatch.setattr(cli, "served_model_path", lambda cfg: str(found[0].path))
    stop, launch = Mock(return_value=True), Mock(return_value=object())
    monkeypatch.setattr(cli, "stop_server_on_port", stop)
    monkeypatch.setattr(cli, "_launch_server_background", launch)
    repl._handle_model(["/model", "gemma"], None)
    assert "already loaded" in capsys.readouterr().out
    repl._handle_model(["/model", "qwen"], None)
    assert "matches several models" in capsys.readouterr().out
    stop.assert_not_called()
    launch.assert_not_called()
    assert config.model_path == str(found[0].path)


def test_failed_restart_or_stop_leaves_configuration_alone(monkeypatch, tmp_path, capsys):
    found = models(tmp_path)
    repl, config = local_repl(found)
    monkeypatch.setattr(cli, "discover_models", lambda extra_dirs=None: found)
    monkeypatch.setattr(cli, "served_model_path", lambda cfg: None)
    launch = Mock(return_value=None)
    monkeypatch.setattr(cli, "stop_server_on_port", Mock(return_value=True))
    monkeypatch.setattr(cli, "_launch_server_background", launch)
    repl._handle_model(["/model", "3"], None)
    assert "did not start" in capsys.readouterr().out
    monkeypatch.setattr(cli, "stop_server_on_port", Mock(return_value=False))
    repl._handle_model(["/model", "3"], None)
    assert "did not stop" in capsys.readouterr().out
    assert launch.call_count == 1
    assert config.model_path == str(found[0].path)
    config.save.assert_not_called()


def test_safetensors_models_point_at_the_conversion_path(monkeypatch, tmp_path, capsys):
    found = models(tmp_path)
    found[2].format = "safetensors"
    repl, config = local_repl(found)
    monkeypatch.setattr(cli, "discover_models", lambda extra_dirs=None: found)
    stop = Mock(return_value=True)
    monkeypatch.setattr(cli, "stop_server_on_port", stop)
    repl._handle_model(["/model", "Qwen3-8B"], None)
    assert "not a GGUF" in capsys.readouterr().out
    stop.assert_not_called()
    config.save.assert_not_called()


def test_workspace_picker_offers_local_models_and_selection_resolves_the_path(monkeypatch, tmp_path, capsys):
    found = models(tmp_path)
    workspace = Mock()
    repl, config = local_repl(found, workspace)
    monkeypatch.setattr(cli, "discover_models", lambda extra_dirs=None: found)
    repl._handle_model(["/model"], None)
    choices, provider, _ = workspace.open_models.call_args.args
    assert provider == "Local" and [c["label"] for c in choices] == [m.name for m in found]
    assert choices[1]["id"] == str(found[1].path) and "gguf" in choices[1]["description"]
    switched = Mock()
    repl._switch_local_model = switched
    repl._handle_model(["/model", choices[1]["id"]], None)
    switched.assert_called_once_with(found[1])
    repl._handle_model(["/model", "list"], None)
    assert workspace.open_models.call_count == 1
