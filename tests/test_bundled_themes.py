"""The shipped OMZ prompts work without installing a framework in the home folder."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import subprocess

import pytest
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input.defaults import create_pipe_input

from tether.core.config import TetherConfig
from tether.ui import theme_catalog as catalog_module
from tether.ui.shell_theme import ShellTheme
from tether.ui.shell_workspace import ShellWorkspace
from .test_shell_workspace import shell_home, until, click, frame
from .test_shell_theme import run_zsh
from .test_terminal_workspace import PaintedOutput, terminal_text


def test_bundle_integrity_and_theme_syntax():
    root = catalog_module.BUNDLED_OMZ
    manifest = json.loads((root / 'manifest.json').read_text())
    assert manifest['commit'] == '2100c630ba4bd67bb48802c3e7da893a4fdaa1a0'
    for name, digest in manifest['files'].items():
        assert hashlib.sha256((root / name).read_bytes()).hexdigest() == digest, name
    themes = sorted((root / 'themes').glob('*.zsh-theme'))
    assert len(themes) == 142
    for path in themes:
        result = subprocess.run(['zsh', '-n', str(path)], capture_output=True, text=True, timeout=3)
        assert result.returncode == 0, f'{path.name}: {result.stderr}'


def test_catalog_is_complete_offline_and_local_custom_themes_win(shell_home, monkeypatch):
    monkeypatch.setattr(catalog_module.Path, 'home', lambda: shell_home)
    choices = {item.id: item for item in catalog_module.theme_catalog()}
    for name in ('agnoster', 'robbyrussell', 'af-magic', 'avit', 'bira', 'random'):
        assert choices['omz:' + name].source == 'Included · Oh My Zsh'
    assert len([item for item in choices.values() if item.source == 'Included · Oh My Zsh']) == 142
    root = shell_home / '.oh-my-zsh'
    (root / 'custom/themes').mkdir(parents=True)
    (root / 'oh-my-zsh.sh').write_text('')
    custom = root / 'custom/themes/agnoster.zsh-theme'
    custom.write_text("PROMPT='LOCAL>'\n")
    choices = {item.id: item for item in catalog_module.theme_catalog()}
    assert choices['omz:agnoster'].path == custom
    assert choices['omz:robbyrussell'].source == 'Included · Oh My Zsh'


@pytest.mark.parametrize('name', ['agnoster', 'robbyrussell', 'af-magic', 'avit', 'bira', 'random'])
def test_bundled_prompts_preserve_startup_and_avoid_framework_side_effects(shell_home, name):
    rc = shell_home / '.zshrc'
    rc.write_text("export THEME_STARTUP=preserved\nalias user_alias='echo keep'\nHISTFILE=''\nPROMPT='OLD>'\n")
    if name == 'random':
        # Test the random theme loader deterministically. Other bundled themes
        # may only set PROMPT in precmd, which zsh -ilc does not invoke.
        with rc.open('a') as stream:
            stream.write('ZSH_THEME_RANDOM_CANDIDATES=(robbyrussell)\n')
    update = shell_home / '.zsh-update'
    update.write_text('must stay here')
    original = {p.name: p.read_bytes() for p in shell_home.iterdir() if p.is_file()}
    output = run_zsh(ShellTheme('zsh', 'omz:' + name), dict(os.environ),
        'print -r -- "STARTUP:$THEME_STARTUP"; print -r -- "ALIAS:$aliases[user_alias]"; '
        'print -r -- "HISTORY:$HISTFILE"; print -r -- "PROMPT:$PROMPT"')
    assert 'STARTUP:preserved' in output and 'ALIAS:echo keep' in output and 'HISTORY:\n' in output
    assert 'PROMPT:OLD>' not in output and output.split('PROMPT:', 1)[1].strip()
    if name == 'random':
        assert "Random theme 'robbyrussell' loaded" in output
    assert not (shell_home / '.oh-my-zsh').exists()
    assert original == {p.name: p.read_bytes() for p in shell_home.iterdir() if p.is_file()}
    assert not (shell_home / '.cache').exists()


@pytest.mark.parametrize('name', ['agnoster', 'robbyrussell'])
@pytest.mark.parametrize('size', [(20, 60), (32, 110)])
def test_bundled_theme_preview_and_save_in_real_pty(shell_home, monkeypatch, name, size):
    import tether.core.config as config_module
    monkeypatch.setattr(config_module, 'CONFIG_FILE', shell_home / 'config.json')
    config = TetherConfig(terminal_shell='zsh')
    config.save()
    original = (shell_home / '.zshrc').read_bytes()

    async def run():
        with create_pipe_input() as pipe:
            output = PaintedOutput()
            output.size = Size(rows=size[0], columns=size[1])
            ui = ShellWorkspace(config, cwd=shell_home, input=pipe, output=output)
            running = asyncio.create_task(ui.run_async())
            try:
                await until(lambda: ui.active and 'READY>' in terminal_text(ui.active.panes.active))
                first = ui.active
                await click(ui, 'Settings')
                pipe.send_text(name)
                await until(lambda: ui.dialog.theme.current_value == 'omz:' + name)
                assert 'Window too small' not in '\n'.join(frame(ui))
                await click(ui, 'Preview')
                await until(lambda: ui.active is not first and ui.active.draft)
                preview = ui.active
                pipe.send_text("printf 'THEME_%s\\n' READY\r")
                await until(lambda: 'THEME_READY' in terminal_text(preview.panes.active))
                assert config.terminal_theme == 'inherit'
                await click(ui, 'Use theme')
                await until(lambda: config.terminal_theme == 'omz:' + name)
                assert TetherConfig.load().terminal_theme == 'omz:' + name
                ui.focus_tab(first)
                pipe.send_text("printf 'ORIGINAL_%s\\n' ALIVE\r")
                await until(lambda: 'ORIGINAL_ALIVE' in terminal_text(first.panes.active))
                assert (shell_home / '.zshrc').read_bytes() == original
            finally:
                if not running.done():
                    ui.app.exit()
                    await running
            assert not ui.backends
    asyncio.run(run())
