"""Discover installed themes without sourcing shell code or changing startup files."""
from __future__ import annotations

from dataclasses import dataclass
from itertools import islice
import os
from pathlib import Path


@dataclass(frozen=True)
class ThemeChoice:
    id: str
    name: str
    source: str
    path: Path | None = None
    framework: Path | None = None


def theme_catalog() -> list[ThemeChoice]:
    choices = [ThemeChoice(key, name, "Built in") for key, name in (
        ("inherit", "Existing shell theme"), ("minimal", "Minimal"),
        ("powerline", "Powerline"), ("custom", "Custom file"),
    )]
    roots = list(dict.fromkeys(Path(value).expanduser() for value in (
        os.environ.get("ZSH", ""), str(Path.home() / ".oh-my-zsh"),
        "/opt/homebrew/share/oh-my-zsh", "/usr/share/oh-my-zsh",
    ) if value))
    installed = {}
    for root in roots:
        if not (root / "oh-my-zsh.sh").is_file():
            continue
        custom = Path(os.environ.get("ZSH_CUSTOM") or root / "custom").expanduser()
        # Match Oh My Zsh's custom-theme precedence. One subdirectory supports
        # common installs such as custom/themes/powerlevel10k/powerlevel10k.
        for directory, source in ((custom, "Custom · Oh My Zsh"),
                                  (custom / "themes", "Custom · Oh My Zsh"),
                                  (root / "themes", "Oh My Zsh")):
            for pattern in ("*.zsh-theme", "*/*.zsh-theme") if directory == custom / "themes" else ("*.zsh-theme",):
                for path in sorted(islice(directory.glob(pattern), 2000)):
                    if len(installed) >= 2000:
                        break
                    if not path.is_file() or not os.access(path, os.R_OK):
                        continue
                    name = path.relative_to(directory).as_posix().removesuffix(".zsh-theme")
                    key = "omz:" + name
                    installed.setdefault(key, ThemeChoice(key, name, source, path.resolve(), root.resolve()))
    return choices + sorted(installed.values(), key=lambda item: item.name.casefold())


def find_theme(theme: str) -> ThemeChoice:
    for item in theme_catalog():
        if item.id == theme:
            return item
    raise ValueError(f"Theme is not installed: {theme}. Choose a theme from Settings or tether theme list.")
