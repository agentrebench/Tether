"""A small, correct s-expression reader/printer.

The spec's representation idiom (``docs/codebase-mental-model/08-representation.md``)
is s-expressions, and compiled invariant checks are stored *as* s-exprs. This
module is the real syntactic layer behind that: it parses the forms and prints
them back, with a :class:`Symbol` type so symbols round-trip distinctly from
strings. Evaluation of compiled checks lives in :mod:`.invariants` (it needs
graph access); this module is purely syntax.

Supported:
  * lists ``(a b c)``
  * symbols ``forbidden-edge``, ``dominated-by`` (kebab-case, ``:keywords``)
  * strings ``"abc"`` (with ``\\n``/``\\t``/``\\"`` escapes)
  * integers / floats
  * booleans ``t`` / ``nil`` → ``True`` / ``None`` is intentionally NOT done;
    ``nil`` reads as the symbol ``nil`` so checks can pattern-match it. Numeric
    and string literals coerce; everything else stays a :class:`Symbol`.
  * quote sugar ``'x`` → ``(quote x)``
"""
from __future__ import annotations

import re
from dataclasses import dataclass


class SExprError(ValueError):
    """Raised on malformed s-expression input."""


@dataclass(frozen=True)
class Symbol:
    name: str

    def __str__(self) -> str:
        return self.name


# A parsed form is: Symbol | str | int | float | list[form]
Form = object


# --------------------------------------------------------------------------
# Reader
# --------------------------------------------------------------------------
def _tokenize(text: str) -> list[str]:
    tokens: list[str] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch in " \t\r\n":
            i += 1
            continue
        if ch == ";":  # line comment
            while i < n and text[i] != "\n":
                i += 1
            continue
        if ch in "()'":
            tokens.append(ch)
            i += 1
            continue
        if ch == '"':
            j = i + 1
            buf = ['"']
            while j < n:
                c = text[j]
                if c == "\\" and j + 1 < n:
                    buf.append(text[j:j + 2])
                    j += 2
                    continue
                buf.append(c)
                j += 1
                if c == '"':
                    break
            else:
                raise SExprError("unterminated string literal")
            tokens.append("".join(buf))
            i = j
            continue
        # bare atom: read until whitespace or paren/quote
        j = i
        while j < n and text[j] not in " \t\r\n()'\";":
            j += 1
        tokens.append(text[i:j])
        i = j
    return tokens


_NUMBER_RE = re.compile(r"[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?")

_UNESCAPES = {"n": "\n", "t": "\t", '"': '"', "\\": "\\"}


def _unescape(s: str) -> str:
    # Single left-to-right pass so an escaped backslash followed by ``n``
    # (e.g. ``C:\\new`` written as ``C:\\\\new``) is not re-decoded as a newline.
    out: list[str] = []
    i, n = 0, len(s)
    while i < n:
        c = s[i]
        if c == "\\" and i + 1 < n:
            out.append(_UNESCAPES.get(s[i + 1], "\\" + s[i + 1]))
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _atom(tok: str) -> Form:
    if tok.startswith('"') and tok.endswith('"') and len(tok) >= 2:
        return _unescape(tok[1:-1])
    # Only numeric-looking tokens become numbers; ``float("inf")``/``nan``
    # would otherwise swallow legitimate symbols.
    if _NUMBER_RE.fullmatch(tok):
        try:
            return int(tok)
        except ValueError:
            return float(tok)
    return Symbol(tok)


def _read_from(tokens: list[str], pos: int) -> tuple[Form, int]:
    if pos >= len(tokens):
        raise SExprError("unexpected end of input")
    tok = tokens[pos]
    if tok == "'":
        form, nxt = _read_from(tokens, pos + 1)
        return [Symbol("quote"), form], nxt
    if tok == "(":
        lst: list[Form] = []
        pos += 1
        while pos < len(tokens) and tokens[pos] != ")":
            form, pos = _read_from(tokens, pos)
            lst.append(form)
        if pos >= len(tokens):
            raise SExprError("unterminated list")
        return lst, pos + 1  # skip ')'
    if tok == ")":
        raise SExprError("unexpected ')'")
    return _atom(tok), pos + 1


def read(text: str) -> Form:
    """Parse exactly one top-level form. Raises :class:`SExprError` on trailing
    junk or malformed input."""
    tokens = _tokenize(text)
    if not tokens:
        raise SExprError("empty input")
    form, pos = _read_from(tokens, 0)
    if pos != len(tokens):
        raise SExprError(f"trailing tokens after form: {tokens[pos:]!r}")
    return form


def read_all(text: str) -> list[Form]:
    """Parse every top-level form in ``text``."""
    tokens = _tokenize(text)
    forms: list[Form] = []
    pos = 0
    while pos < len(tokens):
        form, pos = _read_from(tokens, pos)
        forms.append(form)
    return forms


def try_read(text: str) -> Form | None:
    """Parse one form, returning ``None`` instead of raising on bad input."""
    try:
        return read(text)
    except SExprError:
        return None


# --------------------------------------------------------------------------
# Printer
# --------------------------------------------------------------------------
def _escape(s: str) -> str:
    return (s.replace("\\", "\\\\").replace('"', '\\"')
             .replace("\n", "\\n").replace("\t", "\\t"))


def write(form: Form) -> str:
    """Render a parsed form back to s-expression text (round-trips :func:`read`)."""
    if isinstance(form, Symbol):
        return form.name
    if isinstance(form, bool):  # before int — bool is an int subclass
        return "t" if form else "nil"
    if isinstance(form, str):
        return f'"{_escape(form)}"'
    if isinstance(form, (int, float)):
        return repr(form)
    if form is None:
        return "nil"
    if isinstance(form, (list, tuple)):
        # quote sugar on output
        if len(form) == 2 and form[0] == Symbol("quote"):
            return "'" + write(form[1])
        return "(" + " ".join(write(x) for x in form) + ")"
    return f'"{_escape(str(form))}"'


def is_form(form: Form, head: str) -> bool:
    """True iff ``form`` is a list whose head is the symbol ``head``."""
    return isinstance(form, list) and bool(form) and form[0] == Symbol(head)
