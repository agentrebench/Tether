"""Colored ASCII banner for Tether."""
from .. import __version__
from .colors import COLOR_ENABLED, BOLD, DIM, RESET, rgb_fg

_ART = [
    "████████╗███████╗████████╗██╗  ██╗███████╗██████╗ ",
    "╚══██╔══╝██╔════╝╚══██╔══╝██║  ██║██╔════╝██╔══██╗",
    "   ██║   █████╗     ██║   ███████║█████╗  ██████╔╝",
    "   ██║   ██╔══╝     ██║   ██╔══██║██╔══╝  ██╔══██╗",
    "   ██║   ███████╗   ██║   ██║  ██║███████╗██║  ██║",
    "   ╚═╝   ╚══════╝   ╚═╝   ╚═╝  ╚═╝╚══════╝╚═╝  ╚═╝",
]

_TAGLINE = "local-first coding agent · durable project knowledge · any model"


# The art font ships box-drawing "shadow" characters (╗ ║ ╚ …) that render
# as cluttered outlines, so strip them to clean solid blocks (spaces
# preserve alignment).
_SHADOW = str.maketrans({c: " " for c in "╔╗╚╝║═╦╩╠╣╬"})


def _plain_art() -> str:
    """Clean monochrome wordmark: bold solid blocks, no gradient."""
    lines = (line.translate(_SHADOW) for line in _ART)
    return "\n".join(f"  {BOLD}{ln}{RESET}" for ln in lines if ln.strip())


def banner_compact() -> str:
    if not COLOR_ENABLED:
        return f"TETHER v{__version__}"
    return f"{rgb_fg(90, 200, 255)}{BOLD}TETHER{RESET} {DIM}v{__version__}{RESET}"


def print_banner(compact: bool = False) -> None:
    if compact:
        print(banner_compact())
        return
    if not COLOR_ENABLED:
        print("\n" + "\n".join(f"  {ln}" for ln in _ART))
        print(f"  {_TAGLINE}\n")
        return
    print("\n" + _plain_art())
    print(f"  {DIM}{_TAGLINE}{RESET}\n")
