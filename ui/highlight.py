"""Syntax highlighting for code output in the terminal."""
from __future__ import annotations

import re

try:
    from pygments import highlight as pyg_highlight
    from pygments.lexers import get_lexer_by_name, guess_lexer
    from pygments.formatters import Terminal256Formatter, TerminalTrueColorFormatter
    HAS_PYGMENTS = True
except ImportError:
    HAS_PYGMENTS = False

from .colors import TRUECOLOR

if HAS_PYGMENTS:
    # Match the terminal's actual capability — 24-bit codes on a 256-color
    # terminal render as garbage, not colors.
    FORMATTER = (TerminalTrueColorFormatter(style="monokai") if TRUECOLOR
                 else Terminal256Formatter(style="monokai"))
else:
    FORMATTER = None


def highlight_code(code: str, language: str = "") -> str:
    """Syntax-highlight a code string for terminal output."""
    if not HAS_PYGMENTS or not code.strip():
        return code

    try:
        if language:
            lexer = get_lexer_by_name(language, stripall=True)
        else:
            lexer = guess_lexer(code)
        return pyg_highlight(code, lexer, FORMATTER).rstrip()
    except Exception:
        return code


def _is_code_line(line: str) -> bool:
    """Heuristic: a line that looks like code (indented or has code-like patterns)."""
    stripped = line.rstrip()
    if not stripped:
        return False
    # Indented by 4+ spaces or tab
    if line.startswith("    ") or line.startswith("\t"):
        return True
    # Common code patterns
    code_patterns = [
        r'^\s*(def |class |import |from |if |for |while |return |raise )',  # python
        r'^\s*(function |const |let |var |export |import )',                  # js/ts
        r'^\s*(fn |let |use |pub |impl |struct |enum )',                     # rust
        r'^\s*[\w_]+\s*[=\(]',                                              # assignment or call
        r'^\s*[\}\{\]\[]',                                                   # braces
        r'^\s*(pip |npm |cargo |git |cd |ls |cat |mkdir |chmod |curl )',     # shell
        r'^\s*(source |export |sudo |apt |brew )',                           # shell
        r'^\s*[\w_]+\.[\w_]+\(',                                             # method call
        r'^\s*#!/',                                                          # shebang
    ]
    for pattern in code_patterns:
        if re.match(pattern, stripped):
            return True
    return False


def highlight_inline(text: str) -> str:
    """Detect code blocks in model output and highlight them.

    After markdown stripping, code blocks appear as:
    1. Fenced blocks (```lang ... ```) that slipped through
    2. Contiguous lines that look like code
    """
    if not HAS_PYGMENTS:
        return text

    lines = text.split("\n")
    result = []
    code_buffer = []
    in_fence = False
    fence_lang = ""

    def flush_code():
        if code_buffer:
            code = "\n".join(code_buffer)
            highlighted = highlight_code(code)
            result.append(highlighted)
            code_buffer.clear()

    i = 0
    while i < len(lines):
        line = lines[i]

        # Handle any remaining markdown fences
        if line.strip().startswith("```"):
            if not in_fence:
                flush_code()
                in_fence = True
                fence_lang = line.strip().lstrip("`").strip()
                i += 1
                continue
            else:
                code = "\n".join(code_buffer)
                result.append(highlight_code(code, fence_lang))
                code_buffer.clear()
                in_fence = False
                fence_lang = ""
                i += 1
                continue

        if in_fence:
            code_buffer.append(line)
            i += 1
            continue

        # Detect contiguous code blocks (indented or code-like)
        if _is_code_line(line):
            code_buffer.append(line)
        else:
            # Blank lines within a code block are ok
            if code_buffer and line.strip() == "":
                # Look ahead — if next non-blank line is code, keep buffering
                j = i + 1
                while j < len(lines) and lines[j].strip() == "":
                    j += 1
                if j < len(lines) and _is_code_line(lines[j]):
                    code_buffer.append(line)
                else:
                    flush_code()
                    result.append(line)
            else:
                flush_code()
                result.append(line)

        i += 1

    flush_code()

    # Flush any remaining fence
    if code_buffer:
        code = "\n".join(code_buffer)
        result.append(highlight_code(code, fence_lang))

    return "\n".join(result)
