import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from prompt_toolkit.application.current import set_app
from prompt_toolkit.data_structures import Size
from prompt_toolkit.formatted_text import to_plain_text
from prompt_toolkit.input.defaults import create_pipe_input

from tether import cli
from tether.core.config import TetherConfig
from tether.ui import agent_picker
from tether.ui.agent_picker import AgentPicker, BouncingLight, agent_choices
from .test_terminal_workspace import PaintedOutput


def test_choices_show_provider_names_and_readiness_without_credentials(monkeypatch):
    monkeypatch.setattr(agent_picker.shutil, "which", lambda _: "/bin/codex")
    config = TetherConfig(provider="custom", api_model="my-model", api_key="private-secret",
                          api_keys={"anthropic": "another-secret"})
    choices = agent_choices(config)
    assert [c.name for c in choices] == ["Claude", "Codex", "GPT", "DeepSeek", "Kimi", "GLM", "Local", "Custom"]
    assert choices[0].status == "API key ready"
    assert choices[-1].model == "my-model" and not choices[-1].setup
    assert "private-secret" not in repr(choices) and "another-secret" not in repr(choices)


def test_light_moves_and_bounces_within_its_window():
    now = [0.0]
    light = BouncingLight(lambda: now[0])
    positions = []
    for second in (0, .3, 1, 3, 10, 60):
        now[0] = second
        x, y = light.position(54, 7)
        assert 0 <= x < 54 and 0 <= y < 7
        positions.append((x, y))
        frame = light.create_content(54, 7)
        assert sum(to_plain_text(frame.get_line(row)).count("●") for row in range(7)) == 1
    assert len(set(positions)) > 3


@pytest.mark.parametrize("rows,columns", [(44, 140), (24, 80), (20, 60)])
def test_picker_is_centered_and_paints_the_entire_terminal(rows, columns):
    async def render():
        with create_pipe_input() as pipe:
            output = PaintedOutput()
            output.size = Size(rows=rows, columns=columns)
            picker = AgentPicker(TetherConfig(provider="local"), input=pipe, output=output, clock=lambda: 0)
            with set_app(picker.app):
                picker.app.renderer.render(picker.app, picker.app.layout)
                screen = picker.app.renderer.last_rendered_screen
                lines = ["".join(screen.data_buffer[y][x].char for x in range(columns)) for y in range(rows)]
                title_row = next(i for i, line in enumerate(lines) if "Select your agent" in line)
                title_column = lines[title_row].index("Select your agent")
                assert abs(title_column + len("Select your agent") / 2 - columns / 2) <= 1
                assert "Enter start" in "\n".join(lines)
                top = next(i for i, line in enumerate(lines) if "TETHER" in line)
                bottom = next(i for i, line in enumerate(lines) if "Enter start" in line)
                assert abs(top - (rows - bottom - 1)) <= 1
                for position in ((0, 0), (columns - 1, rows - 1)):
                    assert output.cells[position][1].bgcolor == "101116"
                await picker.app.cancel_and_wait_for_background_tasks()
    asyncio.run(render())


def test_keyboard_selection_and_missing_key_stay_in_terminal(monkeypatch):
    monkeypatch.setattr(TetherConfig, "has_api_key", lambda self, provider=None: provider != "anthropic")
    monkeypatch.setattr(agent_picker.shutil, "which", lambda _: "/bin/codex")

    async def run():
        with create_pipe_input() as pipe:
            picker = AgentPicker(TetherConfig(provider="anthropic"), input=pipe, output=PaintedOutput())
            running = asyncio.create_task(picker.app.run_async())
            try:
                await asyncio.sleep(.05)
                pipe.send_text("\r")
                await asyncio.sleep(.1)
                assert not running.done()
                assert "ANTHROPIC_API_KEY" in picker.message
                pipe.send_text("\x1b[B\r")
                assert await asyncio.wait_for(running, 2) == "codex"
            finally:
                if not running.done():
                    picker.app.exit(result=None)
                    await running
    asyncio.run(run())


@pytest.mark.parametrize("keys", ["\x1b", "\x03", "\x04"])
def test_cancel_never_starts_a_chat(keys):
    async def run():
        with create_pipe_input() as pipe:
            picker = AgentPicker(TetherConfig(provider="local"), input=pipe, output=PaintedOutput())
            pipe.send_text(keys)
            assert await asyncio.wait_for(picker.app.run_async(), 2) is None
    asyncio.run(run())


def test_settings_keyboard_navigation_and_escape_return_to_welcome():
    async def run():
        with create_pipe_input() as pipe:
            picker = AgentPicker(TetherConfig(provider="local"), input=pipe, output=PaintedOutput())
            running = asyncio.create_task(picker.app.run_async())
            try:
                await asyncio.sleep(.05)
                pipe.send_text("\x1bOQ")  # F2
                await asyncio.sleep(.1)
                assert picker.dialog is not None and not running.done()
                selected = picker.selected
                pipe.send_text("\t")
                await asyncio.sleep(.05)
                assert picker.app.layout.has_focus(picker.dialog.theme)
                assert picker.selected == selected
                pipe.send_text("\x1b")
                await asyncio.sleep(.2)
                assert picker.dialog is None and not running.done()
                assert picker.app.layout.has_focus(picker.menu)
                pipe.send_text("\r")
                assert await asyncio.wait_for(running, 2) == "local"
            finally:
                if not running.done():
                    picker.app.exit(result=None)
                    await running
    asyncio.run(run())


def test_selection_applies_to_current_session_and_preserves_existing_model(monkeypatch):
    config = TetherConfig(provider="custom", api_model="custom-model", api_base_url="http://example.test",
                          api_key="custom-key")
    saved = deepcopy(config.__dict__)
    monkeypatch.setattr(AgentPicker, "run", lambda self: "custom")
    assert agent_picker.select_agent(config)
    assert config.__dict__ == saved
    monkeypatch.setattr(AgentPicker, "run", lambda self: "anthropic")
    monkeypatch.setattr(TetherConfig, "save", lambda self: pytest.fail("Menu must not change desktop's saved provider"))
    assert agent_picker.select_agent(config)
    assert config.provider == "anthropic" and config.api_model.startswith("claude-")
    assert config.api_keys["custom"] == "custom-key"


@pytest.mark.parametrize("plain,model,resume,tty,term", [
    (True, None, "", True, "xterm-256color"),
    (False, "model.gguf", "", True, "xterm-256color"),
    (False, None, "session", True, "xterm-256color"),
    (False, None, "", False, "xterm-256color"),
    (False, None, "", True, "dumb"),
])
def test_direct_launches_do_not_prompt(monkeypatch, plain, model, resume, tty, term):
    monkeypatch.setattr(cli.sys, "stdin", SimpleNamespace(isatty=lambda: tty))
    monkeypatch.setattr(cli.sys, "stdout", SimpleNamespace(isatty=lambda: tty))
    monkeypatch.setenv("TERM", term)
    monkeypatch.setattr(agent_picker, "select_agent", lambda config: pytest.fail("Unexpected picker"))
    args = SimpleNamespace(plain=plain, model=model, resume=resume)
    assert cli._select_terminal_agent(args, TetherConfig())


@pytest.mark.parametrize("argv", [["chat"], ["start"], ["run"]])
def test_cli_shows_menu_before_auth_or_server_start(monkeypatch, argv):
    monkeypatch.setattr(cli.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(cli.sys, "stdout", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setattr(cli, "setup_logging", lambda: None)
    monkeypatch.setattr(TetherConfig, "load", lambda: TetherConfig(provider="local"))
    pick = Mock(return_value=False)
    monkeypatch.setattr(agent_picker, "select_agent", pick)
    monkeypatch.setattr(cli, "server_is_running", lambda config: pytest.fail("Server started before selection"))
    assert cli.main(argv) == 0
    pick.assert_called_once()


@pytest.mark.parametrize("compact", [False, True])
def test_selected_provider_reaches_terminal_chat(monkeypatch, compact):
    from tether.ui import repl

    monkeypatch.setattr(cli.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(cli.sys, "stdout", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setattr(cli, "setup_logging", lambda: None)
    config = TetherConfig(provider="anthropic", api_model="saved-model", api_keys={"openai": "test-key"})
    monkeypatch.setattr(TetherConfig, "load", lambda: config)
    monkeypatch.setattr(AgentPicker, "run", lambda self: "openai")
    chat = Mock()
    chat.return_value.run.return_value = 0
    monkeypatch.setattr(repl, "TetherREPL", chat)
    assert cli.main(["chat", "--compact"] if compact else ["chat"]) == 0
    assert config.provider == "openai" and config.api_model != "saved-model"
    if compact:
        chat.assert_called_once_with(config, plain=True, compact=True)
    else:
        chat.assert_called_once_with(config, plain=False)
    chat.return_value.run.assert_called_once_with(resume="")
