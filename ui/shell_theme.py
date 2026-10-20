"""Per-pane zsh themes without editing the user's shell startup files."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import tempfile
from dataclasses import replace
from .theme_catalog import find_theme

THEMES = ("inherit", "minimal", "powerline", "custom")
FONT_MODES = ("ascii", "powerline", "nerd")


def save_settings(config, shell, theme, font, theme_file):
    """Persist first so a failed save cannot change the active settings."""
    from ..core.config import CONFIG_FILE

    validate_settings(shell, theme, font, theme_file)
    # Agent tabs may have saved a model/provider since this workspace opened.
    # Appearance settings must not write those unrelated preferences back stale.
    latest = type(config).load() if CONFIG_FILE.exists() else config
    updated = replace(latest, terminal_shell=shell, terminal_theme=theme,
                      terminal_font_mode=font,
                      terminal_theme_file=str(Path(theme_file).expanduser().resolve()) if theme_file else "")
    updated.save()
    for key in ("terminal_shell", "terminal_theme", "terminal_font_mode", "terminal_theme_file"):
        setattr(config, key, getattr(updated, key))


def resolve_shell(shell: str = "", theme: str = "inherit") -> str:
    candidate = shell or ("zsh" if theme != "inherit" else os.environ.get("SHELL") or "/bin/sh")
    resolved = shutil.which(candidate)
    if not resolved and not shell and theme == "inherit":
        resolved = shutil.which("/bin/sh")
    if not resolved:
        raise ValueError(f"Shell not found or not executable: {candidate}")
    if theme != "inherit" and Path(resolved).name != "zsh":
        raise ValueError("Tether themes require zsh. Choose zsh or the inherit theme.")
    return resolved


def validate_settings(shell: str, theme: str, font: str, theme_file: str = "") -> str:
    if theme not in THEMES:
        find_theme(theme)
    if font not in FONT_MODES:
        raise ValueError(f"Unknown font mode: {font}")
    resolved = resolve_shell(shell, theme)
    if theme == "custom":
        path = Path(theme_file).expanduser()
        if not theme_file or not path.is_file() or not os.access(path, os.R_OK):
            raise ValueError("Choose a readable local zsh theme file.")
    return resolved


def prompt_source(theme: str, font: str) -> str:
    """Static zsh prompt escapes: no subprocesses or interpolated path text."""
    if theme == "minimal":
        prompt = "%(?.%F{green}.%F{red})%n@%m%f %F{cyan}%~%f %(?..%F{red}[%?]%f )%# "
    elif font == "ascii":
        prompt = "%K{blue}%F{white} %n@%m %k%f > %K{cyan}%F{black} %~ %k%f %(?..%F{red}[%?]%f )%# "
    else:
        folder = "\uf07b " if font == "nerd" else ""
        prompt = ("%K{blue}%F{white} %n@%m %K{cyan}%F{blue}\ue0b0%F{black} "
                  + folder + "%~ %k%F{cyan}\ue0b0%f %(?..%F{red}[%?]%f )%# ")
    # Explicitly disable command substitution for built-in prompts; existing
    # user themes can opt into it by choosing inherit or custom instead.
    return ("autoload -Uz add-zsh-hook\n_tether_prompt() {\n"
            "  unsetopt promptsubst promptbang\n  setopt promptpercent\n"
            "  RPROMPT=''\n  PROMPT='" + prompt + "'\n}\n"
            "add-zsh-hook precmd _tether_prompt\n_tether_prompt\n")


class ShellTheme:
    """Own the private startup directory for exactly one child process."""

    def __init__(self, shell: str = "", theme: str = "inherit", font: str = "ascii", theme_file: str = ""):
        self.shell = validate_settings(shell, theme, font, theme_file)
        self.theme, self.font = theme, font
        self.theme_file = str(Path(theme_file).expanduser().resolve()) if theme_file else ""
        self._directory = None

    def environment(self, environment: dict[str, str]) -> dict[str, str]:
        env = dict(environment)
        if self.theme == "inherit":
            return env
        if self._directory is not None:
            raise RuntimeError("Shell theme already started")
        self._directory = tempfile.TemporaryDirectory(prefix="tether-zsh-")
        root = Path(self._directory.name)
        env["TETHER_ZSH_BOOTSTRAP"] = str(root)
        env["TETHER_ZSH_USER_DIR"] = env.get("ZDOTDIR", env.get("HOME", str(Path.home())))
        env["TETHER_ZSH_USER_DIR_SET"] = "1" if "ZDOTDIR" in env else "0"
        env["ZDOTDIR"] = str(root)
        # Source startup files with their normal ZDOTDIR, remembering changes
        # made by .zshenv/.zprofile before forwarding the next file. The private
        # directory is restored only between zsh's startup stages.
        for name in (".zshenv", ".zprofile", ".zshrc", ".zlogin"):
            source = (
                'if [[ $TETHER_ZSH_USER_DIR_SET == 1 ]]; then\n'
                '  export ZDOTDIR="$TETHER_ZSH_USER_DIR"\nelse\n  unset ZDOTDIR\nfi\n'
                f'[[ ! -r "${{ZDOTDIR-$HOME}}/{name}" ]] || source "${{ZDOTDIR-$HOME}}/{name}"\n'
            )
            if name == ".zlogin":
                source += 'source "$TETHER_ZSH_BOOTSTRAP/theme.zsh"\n'
                source += 'unset TETHER_ZSH_BOOTSTRAP TETHER_ZSH_USER_DIR TETHER_ZSH_USER_DIR_SET\n'
            else:
                source += (
                    'TETHER_ZSH_USER_DIR="${ZDOTDIR-$HOME}"\n'
                    'TETHER_ZSH_USER_DIR_SET="${+ZDOTDIR}"\n'
                    'export ZDOTDIR="$TETHER_ZSH_BOOTSTRAP"\n'
                )
            (root / name).write_text(source)
        if self.theme.startswith("omz:"):
            choice = find_theme(self.theme)
            env["TETHER_ZSH_THEME_FILE"] = str(choice.path)
            env["TETHER_OMZ_ROOT"] = str(choice.framework)
            # Usually the user's startup already loaded OMZ. For a fresh zsh
            # config, initialize the installed framework with private caches
            # and no plugins or automatic update checks.
            source = '''if (( ! $+functions[git_prompt_info] )); then
  ZSH="$TETHER_OMZ_ROOT"
  ZSH_THEME=''
  ZSH_CACHE_DIR="$TETHER_ZSH_BOOTSTRAP/cache"
  ZSH_COMPDUMP="$TETHER_ZSH_BOOTSTRAP/zcompdump"
  mkdir -p -- "$ZSH_CACHE_DIR"
  DISABLE_AUTO_UPDATE=true
  zstyle ':omz:update' mode disabled
  plugins=()
  source "$ZSH/oh-my-zsh.sh"
fi
autoload -Uz colors add-zsh-hook
colors
setopt promptsubst promptpercent
_tether_old_precmd=${functions[precmd]-}
_tether_old_hooks="${(j: :)precmd_functions}"
PROMPT='' RPROMPT=''
source "$TETHER_ZSH_THEME_FILE"
# Static themes still need to win over a previous theme's precmd hook.
# Themes which install their own hooks retain their dynamic prompt behavior.
if [[ ${functions[precmd]-} == "$_tether_old_precmd" && "${(j: :)precmd_functions}" == "$_tether_old_hooks" ]]; then
  _tether_theme_left=$PROMPT _tether_theme_right=$RPROMPT
  _tether_selected_prompt() { PROMPT=$_tether_theme_left; RPROMPT=$_tether_theme_right; setopt promptsubst; }
  add-zsh-hook precmd _tether_selected_prompt
fi
unset _tether_old_precmd _tether_old_hooks TETHER_ZSH_THEME_FILE TETHER_OMZ_ROOT
'''
        elif self.theme == "custom":
            # A filename is data, never shell syntax (including quotes/$/spaces).
            env["TETHER_ZSH_THEME_FILE"] = self.theme_file
            source = 'source "$TETHER_ZSH_THEME_FILE"\nunset TETHER_ZSH_THEME_FILE\n'
        else:
            source = prompt_source(self.theme, self.font)
        (root / "theme.zsh").write_text(source)
        return env

    def close(self):
        if self._directory is not None:
            self._directory.cleanup()
            self._directory = None
