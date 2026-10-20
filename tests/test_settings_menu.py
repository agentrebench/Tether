"""Click the rendered settings controls without typing terminal commands."""
from copy import deepcopy

import pytest
from prompt_toolkit.application.current import set_app
from prompt_toolkit.data_structures import Point, Size
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType

from tether.core.config import TetherConfig
from tether.ui.agent_picker import AgentPicker
from tether.ui.workspace import TerminalWorkspace
from .test_terminal_workspace import PaintedOutput, make_repl, render_frame


def click(ui, text, *, last=False):
    render_frame(ui)
    ui.app.layout.update_parents_relations()
    screen = ui.app.renderer.last_rendered_screen
    size = ui.app.output.get_size()
    positions = []
    for y in range(size.rows):
        line = "".join(screen.data_buffer[y][x].char for x in range(size.columns))
        start = 0
        while (x := line.find(text, start)) >= 0:
            positions.append((x, y))
            start = x + len(text)
    assert positions, f"Missing rendered control: {text}"
    x, y = positions[-1 if last else 0]
    event = MouseEvent(Point(x, y), MouseEventType.MOUSE_UP, MouseButton.LEFT, frozenset())
    with set_app(ui.app):
        ui.app.renderer.mouse_handlers.mouse_handlers[y][x](event)


@pytest.mark.parametrize("surface", ["welcome", "workspace"])
def test_click_settings_select_theme_and_font_then_save(surface, monkeypatch, tmp_path):
    import tether.core.config as module
    monkeypatch.setattr(module, "CONFIG_FILE", tmp_path / "config.json")
    with create_pipe_input() as pipe:
        output = PaintedOutput()
        output.size = Size(rows=20, columns=60)
        if surface == "welcome":
            config = TetherConfig(provider="local")
            ui = AgentPicker(config, input=pipe, output=output)
        else:
            repl = make_repl()
            config = repl.config
            ui = TerminalWorkspace(repl, input=pipe, output=output)
        before_provider = config.provider
        click(ui, "Settings")
        if surface == "workspace":
            click(ui, "Shell & appearance")
        click(ui, "( ) Zsh")
        click(ui, "( ) Powerline")
        click(ui, "Nerd Font")
        assert ui.dialog.shell_choice.current_value == "zsh"
        assert ui.dialog.theme.current_value == "powerline"
        assert ui.dialog.font.current_value == "nerd"
        assert "\uf07b" in ui.dialog.preview()
        click(ui, "Save", last=True)
        assert ui.dialog is None
        saved = TetherConfig.load()
        assert saved.terminal_theme == config.terminal_theme == "powerline"
        assert saved.terminal_font_mode == config.terminal_font_mode == "nerd"
        assert saved.terminal_shell == "zsh" and config.provider == before_provider


def test_cancel_and_failed_save_preserve_configuration(monkeypatch, tmp_path):
    import tether.core.config as module
    monkeypatch.setattr(module, "CONFIG_FILE", tmp_path / "config.json")
    config = TetherConfig()
    config.save()
    before = deepcopy(config.__dict__)
    stored = module.CONFIG_FILE.read_bytes()
    with create_pipe_input() as pipe:
        ui = AgentPicker(config, input=pipe, output=PaintedOutput())
        click(ui, "Settings")
        click(ui, "Minimal")
        click(ui, "Cancel")
        assert ui.dialog is None and config.__dict__ == before
        click(ui, "Settings")
        click(ui, "Minimal")
        def fail_save(self):
            raise OSError("Cannot write config")
        monkeypatch.setattr(TetherConfig, "save", fail_save)
        click(ui, "Save", last=True)
        assert "Cannot write config" in ui.dialog.message
        assert config.__dict__ == before and module.CONFIG_FILE.read_bytes() == stored


def test_custom_shell_and_theme_fields_fit_small_terminal():
    with create_pipe_input() as pipe:
        output = PaintedOutput()
        output.size = Size(rows=20, columns=60)
        ui = AgentPicker(TetherConfig(terminal_shell="/bin/zsh", terminal_theme="custom"),
                         input=pipe, output=output)
        click(ui, "Settings")
        render_frame(ui)
        screen = ui.app.renderer.last_rendered_screen
        text = "\n".join("".join(screen.data_buffer[y][x].char for x in range(60)) for y in range(20))
        for label in ("Shell executable path", "Custom theme file", "ASCII", "Nerd Font", "Save", "Cancel"):
            assert label in text
        assert "Window too small" not in text


@pytest.mark.parametrize("title,command", [("Model", "/model"), ("Reasoning effort", "/effort")])
def test_model_settings_use_existing_command_queue_and_block_during_work(title, command):
    with create_pipe_input() as pipe:
        repl = make_repl()
        repl.config = TetherConfig(provider="openai", api_model="gpt-5.6-sol", reasoning_effort="high")
        ui = TerminalWorkspace(repl, input=pipe, output=PaintedOutput())
        ui.update_agent(dict(agent_id="background", status="running", revision=1))
        click(ui, "Settings")
        click(ui, title)
        assert "Finish or stop" in ui.dialog.message and ui.inbox.empty()
        ui.update_agent(dict(agent_id="background", status="completed", revision=2))
        click(ui, title)
        assert ui.dialog is None and ui.inbox.get_nowait() == command
