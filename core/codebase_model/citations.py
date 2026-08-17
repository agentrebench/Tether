"""Content-addressed evidence pointers.

A citation addresses a slice of the *repo* — ``file @ symbol @ commit`` — not the
(disposable) substrate. That's what lets a belief survive deleting the substrate:
re-fetch exactly the cited slice, re-parse it, verify, toss again. See
04-learned-not-decorative.md. The same addressing drives belief invalidation
(:meth:`ModelStore.beliefs_citing_file`) and commit-keyed caching.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Citation:
    file: str
    symbol: str = ""
    commit: str = ""

    def format(self) -> str:
        return format_citation(self.file, self.symbol, self.commit)


@dataclass
class RefetchResult:
    """The outcome of re-fetching a citation's slice for verification."""
    citation: Citation
    found: bool
    source: str = ""
    lineno: int = 0
    end_lineno: int = 0
    error: str = ""


def format_citation(file: str, symbol: str = "", commit: str = "") -> str:
    parts = [file]
    if symbol:
        parts.append(symbol)
    if commit:
        parts.append(commit)
    return " @ ".join(parts)


def parse(raw: str) -> Citation:
    """Parse ``file @ symbol @ commit`` (tolerant of missing trailing parts and
    of surrounding whitespace)."""
    parts = [p.strip() for p in str(raw).split("@")]
    file = parts[0] if parts else ""
    symbol = parts[1] if len(parts) > 1 else ""
    commit = parts[2] if len(parts) > 2 else ""
    return Citation(file=file, symbol=symbol, commit=commit)


def _read_at_commit(repo_root: Path, file: str, commit: str) -> tuple[str | None, str]:
    """Return (content, error). Reads ``file`` at ``commit`` via git, falling
    back to the working-tree copy when commit is empty or git is unavailable."""
    if commit:
        try:
            out = subprocess.run(
                ["git", "-C", str(repo_root), "show", f"{commit}:{file}"],
                capture_output=True, text=True, timeout=10,
            )
            if out.returncode == 0:
                return out.stdout, ""
            # fall through to working tree if the blob isn't found at that commit
        except (OSError, subprocess.SubprocessError) as e:
            return None, f"git show failed: {e}"
    target = repo_root / file
    if not target.is_file():
        return None, f"file not found: {file}"
    try:
        return target.read_text(errors="replace"), ""
    except OSError as e:
        return None, f"read failed: {e}"


def refetch(citation: Citation, repo_root: Path | str) -> RefetchResult:
    """Re-fetch the cited slice and return its source for verification. When a
    symbol is named, returns just that symbol's source; otherwise the whole file.

    This is the operation behind the acceptance test: an agent that *learned*
    can take a belief's citation and pull exactly this slice to check itself
    before editing — no grep, no embeddings."""
    repo_root = Path(repo_root)
    content, err = _read_at_commit(repo_root, citation.file, citation.commit)
    if content is None:
        return RefetchResult(citation=citation, found=False, error=err)

    if not citation.symbol:
        n = content.count("\n") + 1 if content else 0
        return RefetchResult(citation=citation, found=True, source=content,
                             lineno=1, end_lineno=n)

    # Lazy imports to avoid a substrate <-> citations import cycle.
    if citation.file.endswith(".py"):
        from .substrate import extract_symbol_source
        slice_ = extract_symbol_source(content, citation.symbol)
    else:
        from .substrate_generic import extract_symbol_source, language_for
        lang = language_for(citation.file)
        slice_ = extract_symbol_source(content, citation.symbol, lang) if lang else None
    if slice_ is None:
        return RefetchResult(
            citation=citation, found=False,
            error=f"symbol {citation.symbol!r} not found in {citation.file}")
    src, lineno, end_lineno = slice_
    return RefetchResult(citation=citation, found=True, source=src,
                         lineno=lineno, end_lineno=end_lineno)
