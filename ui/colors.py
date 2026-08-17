"""Terminal color utilities."""
import os
import sys


def _color_enabled() -> bool:
    """Whether to emit ANSI. Honors NO_COLOR (https://no-color.org) and turns
    color off when stdout isn't a TTY (piped/redirected output). FORCE_COLOR
    overrides both."""
    if os.environ.get("FORCE_COLOR"):
        return True
    if os.environ.get("NO_COLOR") is not None:
        return False
    try:
        return bool(sys.stdout.isatty())
    except (AttributeError, ValueError):
        return False


COLOR_ENABLED = _color_enabled()

CYAN = "\033[96m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
MAGENTA = "\033[95m"
BLUE = "\033[94m"
BOLD = "\033[1m"
DIM = "\033[2m"
RESET = "\033[0m"
GRAY = "\033[90m"

if not COLOR_ENABLED:
    # Blank every code so the same f-strings emit clean, copy-pasteable text.
    CYAN = GREEN = YELLOW = RED = MAGENTA = BLUE = BOLD = DIM = RESET = GRAY = ""


def colored(text: str, color: str) -> str:
    return f"{color}{text}{RESET}"


def bold(text: str) -> str:
    return f"{BOLD}{text}{RESET}"


def dim(text: str) -> str:
    return f"{DIM}{text}{RESET}"


def success(text: str) -> str:
    return colored(text, GREEN)


def warning(text: str) -> str:
    return colored(text, YELLOW)


def error(text: str) -> str:
    return colored(text, RED)


def info(text: str) -> str:
    return colored(text, CYAN)


def tool_call(name: str) -> str:
    return f"{MAGENTA}{BOLD}{name}{RESET}"


def agent_label(name: str) -> str:
    return f"{BLUE}{BOLD}[{name}]{RESET}"


def muted(text: str) -> str:
    return colored(text, GRAY)


def accent(text: str) -> str:
    return colored(text, BLUE)


# ---------------------------------------------------------------------------
# RGB output with graceful degradation: truecolor when the terminal declares
# it (COLORTERM), otherwise the nearest xterm-256 cube color — so gradients
# render as gradients everywhere instead of raw garbage on older terminals.
# ---------------------------------------------------------------------------

def _truecolor_enabled() -> bool:
    ct = os.environ.get("COLORTERM", "").lower()
    return "truecolor" in ct or "24bit" in ct


TRUECOLOR = _truecolor_enabled()


def _xterm256_index(r: int, g: int, b: int) -> int:
    """Nearest color in the xterm 6x6x6 cube (indices 16-231)."""
    def level(v: int) -> int:
        return 0 if v < 48 else (1 if v < 114 else (v - 35) // 40)
    return 16 + 36 * level(r) + 6 * level(g) + level(b)


def rgb_fg(r: int, g: int, b: int) -> str:
    """Foreground SGR for an RGB color, degrading to 256-color mode."""
    if not COLOR_ENABLED:
        return ""
    if TRUECOLOR:
        return f"\033[38;2;{r};{g};{b}m"
    return f"\033[38;5;{_xterm256_index(r, g, b)}m"


# ---------------------------------------------------------------------------
# Confidence shading — a red -> amber -> green ramp used to shade diff hunks
# by how sure the agent is about an edit.
# ---------------------------------------------------------------------------

def _clamp01(score: float) -> float:
    try:
        s = float(score)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, s))


def _lerp(a: int, b: int, t: float) -> int:
    return int(round(a + (b - a) * t))


def confidence_rgb(score: float) -> tuple[int, int, int]:
    """Map a 0.0-1.0 confidence to an (r, g, b) triple: low=red, mid=amber,
    high=green."""
    s = _clamp01(score)
    if s < 0.5:
        t = s / 0.5  # red -> amber
        return (_lerp(0xD0, 0xD8, t), _lerp(0x30, 0xB0, t), _lerp(0x2F, 0x10, t))
    t = (s - 0.5) / 0.5  # amber -> green
    return (_lerp(0xD8, 0x40, t), _lerp(0xB0, 0xB4, t), _lerp(0x10, 0x55, t))


def confidence_color(score: float) -> str:
    """ANSI SGR prefix for the given confidence (no RESET). Truecolor when
    available, nearest 256-color otherwise."""
    r, g, b = confidence_rgb(score)
    return rgb_fg(r, g, b)


def confidence_label(score: float) -> str:
    s = _clamp01(score)
    if s >= 0.8:
        return "high"
    if s >= 0.5:
        return "medium"
    return "low"


def confidence_bar(score: float, width: int = 10) -> str:
    """A filled/empty block bar for the confidence, e.g. ████░░░░░░."""
    s = _clamp01(score)
    filled = int(round(s * width))
    filled = max(0, min(width, filled))
    return "█" * filled + "░" * (width - filled)
