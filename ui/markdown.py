"""Lightweight terminal markdown rendering for streamed model prose.

Not a full markdown engine — just the inline styling that makes a response read
nicely in a terminal (bold, italic, `inline code`, links) plus ANSI-aware word
wrapping so wrapped lines keep their gutter and don't break mid-word. Block-level
bits (headers, bullets) are handled by the caller; this module owns inline
styling + wrapping.
"""
from __future__ import annotations

import re
import shutil

from .colors import BOLD, BLUE, CYAN, RESET, COLOR_ENABLED

ITALIC = "\033[3m" if COLOR_ENABLED else ""
UNDERLINE = "\033[4m" if COLOR_ENABLED else ""

# One regex matching every inline construct we style.
_INLINE = re.compile(
    r"(\*\*.+?\*\*"          # **bold**
    r"|`[^`]+`"              # `code`
    r"|(?<![\w*])\*[^*\s][^*]*?\*(?![\w*])"  # *italic* (not intraword: 2*3*4)
    r"|(?<!\w)_[^_\s][^_]*?_(?!\w)"        # _italic_ (not intraword: my_var_name)
    r"|\[[^\]]+\]\([^)]+\))"  # [text](url)
)


def term_width(default: int = 80) -> int:
    try:
        return shutil.get_terminal_size((default, 24)).columns
    except OSError:
        return default


def _style_token(tok: str) -> tuple[str, str]:
    """(visible_text, ansi_prefix) for one inline token; ('', '') passthrough."""
    if tok.startswith("**") and tok.endswith("**"):
        return tok[2:-2], BOLD
    if tok.startswith("`") and tok.endswith("`"):
        return tok[1:-1], CYAN
    if (tok.startswith("*") and tok.endswith("*")) or (tok.startswith("_") and tok.endswith("_")):
        return tok[1:-1], ITALIC
    m = re.fullmatch(r"\[([^\]]+)\]\(([^)]+)\)", tok)
    if m:
        return m.group(1), UNDERLINE + BLUE
    return tok, ""


def to_runs(text: str) -> list[tuple[str, str]]:
    """Split text into (visible_text, ansi_prefix) runs."""
    runs: list[tuple[str, str]] = []
    for part in _INLINE.split(text):
        if not part:
            continue
        if _INLINE.fullmatch(part):
            runs.append(_style_token(part))
        else:
            runs.append((part, ""))
    return runs


def _words(runs: list[tuple[str, str]]):
    """Yield (visible_len, rendered) word atoms, breaking on whitespace. A word
    may span several style runs so styling adjacent to text (no space) survives."""
    word: list[tuple[str, str]] = []  # (fragment, code)
    for vis, code in runs:
        for chunk in re.split(r"(\s+)", vis):
            if chunk == "":
                continue
            if chunk.isspace():
                if word:
                    yield _finish(word)
                    word = []
            else:
                word.append((chunk, code))
    if word:
        yield _finish(word)


def _finish(fragments: list[tuple[str, str]]):
    vis = sum(len(f) for f, _ in fragments)
    rendered = "".join(f"{c}{f}{RESET}" if c else f for f, c in fragments)
    return vis, rendered


def wrap_styled(text: str, width: int) -> list[str]:
    """Render inline markdown and greedily word-wrap to `width` visible columns.
    Returns rendered (ANSI) lines, gutter not included."""
    width = max(8, width)
    lines: list[str] = []
    cur: list[str] = []
    cur_len = 0
    for vis, rendered in _words(to_runs(text)):
        add = vis + (1 if cur else 0)
        if cur and cur_len + add > width:
            lines.append(" ".join(cur))
            cur, cur_len = [], 0
            add = vis
        cur.append(rendered)
        cur_len += add
    if cur:
        lines.append(" ".join(cur))
    return lines or [""]
