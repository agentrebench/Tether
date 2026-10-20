"""Real zsh startup, prompt safety, settings, and glyph rendering."""
import os
from pathlib import Path
import shutil
import subprocess

import pytest
from prompt_toolkit.input.defaults import create_pipe_input

from tether.cli import build_parser, cmd_theme
from tether.core.config import TetherConfig
from tether.ui.shell_theme import ShellTheme
from tether.ui.workspace import TerminalWorkspace
from .test_terminal_workspace import PaintedOutput, make_repl, render_frame

pytestmark = pytest.mark.skipif(not shutil.which("zsh"), reason="zsh is not installed")


def run_zsh(theme, environment, command):
    env = theme.environment(environment)
    bootstrap = env.get("TETHER_ZSH_BOOTSTRAP")
    try:
        result = subprocess.run([theme.shell, "-ilc", command], env=env, text=True,
                                capture_output=True, timeout=10)
        assert result.returncode == 0, result.stderr
        return result.stdout
    finally:
        theme.close()
        if bootstrap:
            assert not Path(bootstrap).exists()


@pytest.fixture
def shell_env(tmp_path):
    if not shutil.which("zsh"):
        pytest.skip("zsh is not installed")
    return dict(os.environ, HOME=str(tmp_path), ZDOTDIR=str(tmp_path), TERM="xterm-256color")


def test_startup_order_and_restored_zdotdir(shell_env, tmp_path):
    for name in (".zshenv", ".zprofile", ".zshrc", ".zlogin"):
        (tmp_path / name).write_text(f"print -r -- '{name}'\n")
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    output = run_zsh(ShellTheme("zsh", "minimal"), shell_env,
                     'print -r -- "DIR=$ZDOTDIR"; print -r -- "$PROMPT"; print -r -- "${TETHER_ZSH_BOOTSTRAP-unset}"')
    assert output.splitlines()[:4] == [".zshenv", ".zprofile", ".zshrc", ".zlogin"]
    assert f"DIR={tmp_path}" in output and "%~" in output
    assert output.splitlines()[-1] == "unset"
    assert before == {p.name: p.read_bytes() for p in tmp_path.iterdir()}


def test_startup_can_redirect_zdotdir(shell_env, tmp_path):
    config = tmp_path / "zsh settings"
    config.mkdir()
    shell_env["CUSTOM_ZSH_DIR"] = str(config)
    (tmp_path / ".zshenv").write_text('export ZDOTDIR="$CUSTOM_ZSH_DIR"\n')
    (config / ".zshrc").write_text("print 'redirected-rc'\n")
    output = run_zsh(ShellTheme("zsh", "minimal"), shell_env, 'print -r -- "$ZDOTDIR"')
    assert "redirected-rc" in output and output.splitlines()[-1] == str(config)


def test_unset_zdotdir_and_nested_shell_use_normal_home(shell_env, tmp_path):
    shell_env.pop("ZDOTDIR")
    (tmp_path / ".zshrc").write_text("PROMPT='USER-PROMPT'\n")
    output = run_zsh(ShellTheme("zsh", "powerline", "powerline"), shell_env,
                     'print -r -- "${ZDOTDIR-unset}"; zsh -ic \'print -r -- "$PROMPT"\'')
    assert output.splitlines() == ["unset", "USER-PROMPT"]


def test_inherit_preserves_external_theme(shell_env, tmp_path):
    (tmp_path / ".zshrc").write_text("PROMPT='EXISTING-THEME'\n")
    theme = ShellTheme("zsh")
    assert theme.environment(shell_env) == shell_env
    assert run_zsh(theme, shell_env, 'print -r -- "$PROMPT"').strip() == "EXISTING-THEME"


@pytest.mark.parametrize("font,glyph", [("ascii", ">"), ("powerline", "\ue0b0"), ("nerd", "\uf07b")])
def test_powerline_prompt_and_ascii_fallback(shell_env, font, glyph):
    output = run_zsh(ShellTheme("zsh", "powerline", font), shell_env, 'print -P -- "$PROMPT"')
    assert glyph in output
    if font == "ascii":
        assert "\ue0b0" not in output and "\uf07b" not in output


def test_custom_theme_filename_is_data(shell_env, tmp_path):
    file = tmp_path / "theme ' $(touch INJECTED).zsh"
    file.write_text("PROMPT='CUSTOM-OK'\n")
    output = run_zsh(ShellTheme("zsh", "custom", theme_file=str(file)), shell_env,
                     'print -r -- "$PROMPT"')
    assert output.strip() == "CUSTOM-OK"
    assert not (Path.cwd() / "INJECTED").exists()


def test_invalid_configuration_rejected_before_pty_allocation():
    with pytest.raises(ValueError, match="require zsh"):
        ShellTheme("sh", "powerline")
    with pytest.raises(ValueError, match="not executable"):
        ShellTheme("/missing/tether-shell")
    with pytest.raises(ValueError, match="readable"):
        ShellTheme("zsh", "custom", theme_file="/missing/theme.zsh")


def test_cli_saves_and_reloads_theme_without_provider_start(monkeypatch, tmp_path):
    import tether.core.config as module
    monkeypatch.setattr(module, "CONFIG_FILE", tmp_path / "config.json")
    parser = build_parser()
    assert cmd_theme(parser.parse_args(["theme", "set", "powerline", "--font", "nerd", "--shell", "zsh"])) == 0
    saved = TetherConfig.load()
    assert saved.terminal_theme == "powerline" and saved.terminal_font_mode == "nerd"
    before = module.CONFIG_FILE.read_bytes()
    assert cmd_theme(parser.parse_args(["theme", "set", "custom", "--file", "/missing"])) == 2
    assert module.CONFIG_FILE.read_bytes() == before


def test_shell_settings_render_at_small_size_and_save(monkeypatch, tmp_path):
    from prompt_toolkit.data_structures import Size
    import tether.core.config as module
    monkeypatch.setattr(module, "CONFIG_FILE", tmp_path / "config.json")
    with create_pipe_input() as pipe:
        output = PaintedOutput()
        output.size = Size(rows=20, columns=60)
        ui = TerminalWorkspace(make_repl(), input=pipe, output=output)
        ui.open_settings()
        ui.open_shell_settings()
        ui.dialog.theme.current_value = "powerline"
        ui.dialog.font.current_value = "powerline"
        render_frame(ui)
        screen = ui.app.renderer.last_rendered_screen
        text = "\n".join("".join(screen.data_buffer[y][x].char for x in range(60)) for y in range(20))
        assert "Save" in text and "Cancel" in text and "Window too small" not in text
        ui.dialog.save()
        assert ui.dialog is None and TetherConfig.load().terminal_theme == "powerline"


def test_real_themed_pty_survives_existing_prompt_hooks_and_cleans_up(monkeypatch, tmp_path):
    import asyncio
    import time
    from tether.ui.terminal_backend import ShellBackend

    monkeypatch.setenv("ZDOTDIR", str(tmp_path))
    monkeypatch.setenv("TETHER_CONFIG_DIR", str(tmp_path / "config"))
    (tmp_path / ".zshrc").write_text("precmd() { PROMPT='OLD-THEME '; }\n")

    async def check():
        backend = ShellBackend(tmp_path, "zsh", theme="powerline", font="powerline")
        backend.set_size(80, 24)
        backend.start()
        root = Path(backend.theme._directory.name)
        output = ""

        async def until(text):
            nonlocal output
            deadline = time.monotonic() + 5
            while text not in output and time.monotonic() < deadline:
                output = (output + backend.read_text())[-32000:]
                await asyncio.sleep(.01)
            assert text in output, repr(output)

        try:
            await until("\ue0b0")
            assert "OLD-THEME" not in output
            output = ""
            backend.write_text("false\r")
            await until("[1]")
            backend.write_text("exit\r")
            deadline = time.monotonic() + 5
            while not backend.ready_f.done() and time.monotonic() < deadline:
                output = (output + backend.read_text())[-32000:]
                await asyncio.sleep(.01)
            assert backend.ready_f.done(), repr(output)
            assert not root.exists()
        finally:
            backend.kill()
            await asyncio.wait_for(asyncio.shield(backend.ready_f), timeout=5)
        assert backend.closed

    asyncio.run(check())
