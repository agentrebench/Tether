"""Real shell input, clickable themes, and agent-optional startup contracts."""
import asyncio
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from prompt_toolkit.application.current import set_app
from prompt_toolkit.data_structures import Point, Size
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType

from tether import cli
from tether.core.config import TetherConfig
from tether.ui.shell_workspace import ShellWorkspace
from tether.ui.theme_catalog import theme_catalog
from tether.ui.shell_theme import ShellTheme
from .test_terminal_workspace import PaintedOutput, terminal_text
from .test_shell_theme import run_zsh


@pytest.fixture
def shell_home(monkeypatch, tmp_path):
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('ZDOTDIR', str(tmp_path))
    monkeypatch.setenv('SHELL', '/bin/zsh')
    monkeypatch.setenv('TETHER_CONFIG_DIR', str(tmp_path / 'config'))
    monkeypatch.delenv('ZSH_CUSTOM', raising=False)
    monkeypatch.delenv('ZSH', raising=False)
    (tmp_path / '.zshrc').write_text("PROMPT='READY> '; HISTFILE=''; bindkey -e\n")
    return tmp_path


async def until(predicate, timeout=6):
    end = asyncio.get_running_loop().time() + timeout
    while not predicate() and asyncio.get_running_loop().time() < end:
        await asyncio.sleep(.02)
    assert predicate(), 'Timed out waiting for terminal state'


def frame(ui):
    screen = ui.app.renderer.last_rendered_screen
    size = ui.app.output.get_size()
    return [''.join(screen.data_buffer[y][x].char for x in range(size.columns)) for y in range(size.rows)] if screen else []


async def click(ui, text):
    await until(lambda: any(text in row for row in frame(ui)))
    # Allow a newly focused dialog to render its cursor and mouse hit regions.
    await asyncio.sleep(.04)
    y, row = next((y, row) for y, row in enumerate(frame(ui)) if text in row)
    x = row.index(text)
    event = MouseEvent(Point(x, y), MouseEventType.MOUSE_UP, MouseButton.LEFT, frozenset())
    with set_app(ui.app):
        ui.app.renderer.mouse_handlers.mouse_handlers[y][x](event)
    ui.invalidate()


@pytest.mark.parametrize('argv', [[], ['terminal']])
def test_default_launch_does_not_ask_for_provider(monkeypatch, argv):
    monkeypatch.setattr(cli.sys, 'stdin', SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(cli.sys, 'stdout', SimpleNamespace(isatty=lambda: True))
    monkeypatch.setenv('TERM', 'xterm-256color')
    monkeypatch.setattr(cli, 'setup_logging', lambda: None)
    monkeypatch.setattr(cli, 'cmd_start', lambda _: pytest.fail('Agent startup from normal terminal'))
    opened = Mock(return_value=0)
    monkeypatch.setattr(cli, 'cmd_terminal', opened)
    assert cli.main(argv) == 0
    opened.assert_called_once()


@pytest.mark.parametrize('argv', [['--plain'], ['--continue'], ['--continue', 'saved-session']])
def test_explicit_chat_modes_remain_available(monkeypatch, argv):
    monkeypatch.setattr(cli.sys, 'stdin', SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(cli.sys, 'stdout', SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(cli, 'setup_logging', lambda: None)
    monkeypatch.setattr(cli, 'cmd_terminal', lambda _: pytest.fail('Explicit chat redirected'))
    monkeypatch.setattr(cli, 'cmd_start', lambda _: 17)
    assert cli.main(argv) == 17


def make_omz(home):
    root = home / '.oh-my-zsh'
    (root / 'themes').mkdir(parents=True)
    (root / 'custom' / 'themes').mkdir(parents=True)
    (root / 'oh-my-zsh.sh').write_text('git_prompt_info() { print git; }\n')
    (root / 'themes' / 'ocean.zsh-theme').write_text("PROMPT='OCEAN $(git_prompt_info)> '\n")
    (root / 'themes' / 'second.zsh-theme').write_text("PROMPT='SECOND> '\n")
    return root


def test_catalog_is_read_only_and_custom_themes_take_precedence(shell_home):
    root = make_omz(shell_home)
    custom = root / 'custom' / 'themes' / 'ocean.zsh-theme'
    custom.write_text("PROMPT='CUSTOM> '\ntouch SHOULD_NOT_RUN\n")
    nested = root / 'custom' / 'themes' / 'powerlevel10k'
    nested.mkdir()
    (nested / 'powerlevel10k.zsh-theme').write_text('')
    catalog = {item.id: item for item in theme_catalog()}
    assert catalog['omz:ocean'].path == custom
    assert 'omz:powerlevel10k/powerlevel10k' in catalog
    assert not (Path.cwd() / 'SHOULD_NOT_RUN').exists()


def test_named_theme_loads_framework_and_wins_over_existing_prompt_hook(shell_home):
    make_omz(shell_home)
    rc = shell_home / '.zshrc'
    rc.write_text("precmd() { PROMPT='OLD> '; }\n")
    original = rc.read_bytes()
    output = run_zsh(ShellTheme('zsh', 'omz:ocean'), dict(os.environ),
                     'precmd; for f in $precmd_functions; do $f; done; print -P -- "$PROMPT"')
    assert output.strip() == 'OCEAN git>'
    assert rc.read_bytes() == original


def test_theme_save_preserves_preferences_saved_by_an_agent_tab(monkeypatch, tmp_path):
    import tether.core.config as module
    from tether.ui.shell_theme import save_settings
    monkeypatch.setattr(module, 'CONFIG_FILE', tmp_path / 'config.json')
    workspace_config = TetherConfig(provider='local')
    workspace_config.save()
    changed = TetherConfig.load()
    changed.provider, changed.api_model = 'custom', 'new-model'
    changed.save()
    save_settings(workspace_config, 'zsh', 'minimal', 'ascii', '')
    saved = TetherConfig.load()
    assert (saved.provider, saved.api_model) == ('custom', 'new-model')
    assert saved.terminal_theme == workspace_config.terminal_theme == 'minimal'


@pytest.mark.parametrize('size', [(20, 60), (32, 110)])
def test_shell_commands_settings_preview_save_and_agent_tab(shell_home, monkeypatch, size):
    import tether.core.config as config_module
    monkeypatch.setattr(config_module, 'CONFIG_FILE', shell_home / 'config.json')
    make_omz(shell_home)
    config = TetherConfig(terminal_shell='zsh')
    config.save()
    initial = config_module.CONFIG_FILE.read_bytes()

    async def run():
        with create_pipe_input() as pipe:
            output = PaintedOutput()
            output.size = Size(rows=size[0], columns=size[1])
            ui = ShellWorkspace(config, cwd=shell_home, input=pipe, output=output)
            running = asyncio.create_task(ui.run_async())
            try:
                await until(lambda: ui.active and 'READY>' in terminal_text(ui.active.panes.active))
                first = ui.active
                assert 'Select your agent' not in '\n'.join(frame(ui))
                pipe.send_text("mkdir child; cd child; printf 'shell-%s\\n' working; pwd\r")
                await until(lambda: 'shell-working' in terminal_text(first.panes.active))
                assert 'child' in terminal_text(first.panes.active)
                pipe.send_text('sleep 30\r')
                await asyncio.sleep(.1)
                pipe.send_text("\x03printf 'interrupt-%s\\n' ok\r")
                await until(lambda: 'interrupt-ok' in terminal_text(first.panes.active))
                # Ctrl+W must delete a word in the shell, not swallow input as a split prefix.
                pipe.send_text("printf 'word-%s\\n' wrong\x17right\r")
                await until(lambda: 'word-right' in terminal_text(first.panes.active))
                pipe.send_text("for i in {1..80}; do echo history-$i; done\r")
                await until(lambda: 'history-80' in terminal_text(first.panes.active))
                with set_app(ui.app):
                    first.panes.active.terminal.terminal_control.mouse_handler(
                        MouseEvent(Point(5, 5), MouseEventType.SCROLL_UP, MouseButton.NONE, frozenset()))
                await until(ui.is_scrollback)
                assert 'history-1' in first.panes.active.terminal.copy_buffer.text
                pipe.send_text('\x1b')
                await until(lambda: not ui.is_scrollback())
                pipe.send_text('\x1b[5;2~')  # Shift+PageUp
                await until(ui.is_scrollback)
                pipe.send_text('\x1b')
                await until(lambda: not ui.is_scrollback())
                await click(ui, 'Settings')
                await until(lambda: ui.dialog is not None)
                pipe.send_text('ocean')
                await until(lambda: ui.dialog.theme.current_value == 'omz:ocean')
                assert 'Window too small' not in '\n'.join(frame(ui))
                await click(ui, 'Preview')
                await until(lambda: ui.active is not first and 'OCEAN git>' in terminal_text(ui.active.panes.active))
                assert config_module.CONFIG_FILE.read_bytes() == initial
                preview = ui.active
                await click(ui, 'Use theme')
                await until(lambda: config.terminal_theme == 'omz:ocean')
                assert ui.active is preview and not preview.draft
                assert TetherConfig.load().terminal_theme == 'omz:ocean'
                # Opening an agent is optional and retains the running shell tab.
                await click(ui, 'Agent')
                await until(lambda: len(ui.tabs) == 3)
                agent = ui.active
                assert agent.panes.active.terminal.process.backend.command[-2:] == ['chat', '--compact']
                await until(lambda: 'Select your agent' in terminal_text(agent.panes.active))
                pipe.send_text('\x04')
                await until(lambda: agent not in ui.tabs)
                ui.focus_tab(first)
                await click(ui, 'Split')
                await until(lambda: len(first.panes.leaves()) == 2)
                await until(lambda: 'OCEAN git>' in terminal_text(first.panes.active))
                pipe.send_text('exit\r')
                await until(lambda: len(first.panes.leaves()) == 1)
                pipe.send_text('exit\r')
                await until(lambda: first not in ui.tabs)
                pipe.send_text('exit\r')
                assert await asyncio.wait_for(running, 5) == 0
            finally:
                if not running.done():
                    ui.app.exit()
                    await running
            assert not ui.backends
    asyncio.run(run())


def test_mouse_toggle_hands_selection_back_to_the_terminal(shell_home):
    from prompt_toolkit.keys import Keys
    with create_pipe_input() as pipe:
        ui = ShellWorkspace(TetherConfig(), cwd=shell_home, input=pipe, output=PaintedOutput())
        assert ui.app.mouse_support()
        assert any(binding.keys == (Keys.F10,) for binding in ui.app.key_bindings.bindings)
        assert 'Select' in ''.join(fragment[1] for fragment in ui.actions())
        ui.toggle_mouse()
        assert not ui.app.mouse_support() and 'F10' in ui.notice
        assert 'F10' in ''.join(fragment[1] for fragment in ui.status())
        ui.toggle_mouse()
        assert ui.app.mouse_support() and not ui.notice
