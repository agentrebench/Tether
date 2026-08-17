"""Generic (non-Python) substrate extraction — regex-based, stdlib-only.

Python gets the full AST treatment in :mod:`.substrate`; every other supported
language gets a deliberately lighter pass here: top-level definitions
(functions, classes/types) and import edges, scanned line-by-line with
per-language regexes. No call graph, no nesting — enough for ``owns``,
``architecture_index`` and content-addressed citations to work in polyglot
repos, while ``affects``/blast-radius stays strongest for Python.

Definition spans are heuristic: a definition ends where the next one starts
(or at EOF, capped). That's fine for the two consumers — slice hashing for
invalidation and refetch-for-verification — which only need "the region that
belongs to this symbol", not a parse tree.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .model import Edge, EdgeKind, Node, NodeKind, module_of, node_id
from .substrate import file_content_hash

# A definition longer than this is sliced short for hashing/refetch — the head
# of a symbol is what identifies it.
_MAX_DEF_LINES = 120


@dataclass(frozen=True)
class LangSpec:
    name: str
    # (node kind, compiled regex with a <name> group), tried in order per line
    defs: tuple = ()
    # compiled regexes with a <target> group
    imports: tuple = ()
    # optional (open, item, close) regexes for block imports (go)
    import_block: tuple | None = None


def _spec(name, defs, imports, import_block=None):
    return LangSpec(
        name=name,
        defs=tuple((kind, re.compile(rx)) for kind, rx in defs),
        imports=tuple(re.compile(rx) for rx in imports),
        import_block=tuple(re.compile(rx) for rx in import_block) if import_block else None,
    )


_JS_DEFS = [
    (NodeKind.FUNCTION,
     r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*(?P<name>[A-Za-z_$][\w$]*)"),
    (NodeKind.FUNCTION,
     r"^\s*(?:export\s+)?(?:const|let|var)\s+(?P<name>[A-Za-z_$][\w$]*)\s*=\s*"
     r"(?:async\s*)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>"),
    (NodeKind.CLASS,
     r"^\s*(?:export\s+)?(?:default\s+)?(?:abstract\s+)?class\s+(?P<name>[A-Za-z_$][\w$]*)"),
    (NodeKind.CLASS,
     r"^\s*(?:export\s+)?(?:interface|enum)\s+(?P<name>[A-Za-z_$][\w$]*)"),
]
_JS_IMPORTS = [
    r"""^\s*import\s+(?:[^'"]*?from\s+)?['"](?P<target>[^'"]+)['"]""",
    r"""\brequire\(\s*['"](?P<target>[^'"]+)['"]\s*\)""",
    r"""^\s*export\s+(?:\*|\{[^}]*\})\s+from\s+['"](?P<target>[^'"]+)['"]""",
]

_C_LIKE_FUNC = (
    # A col-0-ish definition line: type tokens, then name(, and no trailing ';'
    r"^[A-Za-z_][\w:<>,\s\*&]*[\s\*&](?P<name>[A-Za-z_]\w*)\s*\([^;]*$"
)

SPECS: dict[str, LangSpec] = {
    "javascript": _spec("javascript", _JS_DEFS, _JS_IMPORTS),
    "typescript": _spec("typescript", _JS_DEFS + [
        (NodeKind.CLASS, r"^\s*(?:export\s+)?type\s+(?P<name>\w+)\s*="),
    ], _JS_IMPORTS),
    "go": _spec(
        "go",
        [(NodeKind.FUNCTION, r"^func\s+(?:\([^)]*\)\s*)?(?P<name>\w+)\s*\("),
         (NodeKind.CLASS, r"^type\s+(?P<name>\w+)\s+(?:struct|interface)\b")],
        [r'^import\s+(?:\w+\s+)?"(?P<target>[^"]+)"'],
        import_block=(r"^import\s*\(", r'^\s*(?:\w+\s+)?"(?P<target>[^"]+)"', r"^\)"),
    ),
    "rust": _spec(
        "rust",
        [(NodeKind.FUNCTION,
          r'^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?(?:unsafe\s+)?(?:extern\s+"[^"]*"\s+)?fn\s+(?P<name>\w+)'),
         (NodeKind.CLASS,
          r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:struct|enum|trait)\s+(?P<name>\w+)")],
        [r"^\s*(?:pub\s+)?use\s+(?P<target>[\w:]+)"],
    ),
    "java": _spec(
        "java",
        [(NodeKind.CLASS,
          r"^\s*(?:@\w+(?:\([^)]*\))?\s+)*(?:public|private|protected|abstract|final|static|sealed|\s)*"
          r"\b(?:class|interface|enum|record)\s+(?P<name>\w+)")],
        [r"^\s*import\s+(?:static\s+)?(?P<target>[\w.*]+)\s*;"],
    ),
    "kotlin": _spec(
        "kotlin",
        [(NodeKind.CLASS,
          r"^\s*(?:@\w+\s+)*(?:public|private|internal|abstract|open|sealed|data|\s)*"
          r"\b(?:class|interface|object|enum\s+class)\s+(?P<name>\w+)"),
         (NodeKind.FUNCTION,
          r"^\s*(?:public|private|internal|open|override|suspend|inline|\s)*\bfun\s+(?:<[^>]+>\s+)?(?P<name>\w+)")],
        [r"^\s*import\s+(?P<target>[\w.*]+)"],
    ),
    "csharp": _spec(
        "csharp",
        [(NodeKind.CLASS,
          r"^\s*(?:\[[^\]]*\]\s*)*(?:public|private|protected|internal|abstract|sealed|static|partial|\s)*"
          r"\b(?:class|interface|enum|struct|record)\s+(?P<name>\w+)")],
        [r"^\s*using\s+(?:static\s+)?(?P<target>[\w.]+)\s*;"],
    ),
    "c": _spec(
        "c",
        [(NodeKind.CLASS, r"^\s*(?:typedef\s+)?(?:struct|enum|union)\s+(?P<name>\w+)"),
         (NodeKind.FUNCTION, _C_LIKE_FUNC)],
        [r'^\s*#\s*include\s*[<"](?P<target>[^>"]+)[>"]'],
    ),
    "cpp": _spec(
        "cpp",
        [(NodeKind.CLASS, r"^\s*(?:template\s*<[^>]*>\s*)?(?:class|struct|enum(?:\s+class)?|union)\s+(?P<name>\w+)"),
         (NodeKind.FUNCTION, _C_LIKE_FUNC)],
        [r'^\s*#\s*include\s*[<"](?P<target>[^>"]+)[>"]'],
    ),
    "ruby": _spec(
        "ruby",
        [(NodeKind.FUNCTION, r"^\s*def\s+(?:self\.)?(?P<name>[\w?!]+)"),
         (NodeKind.CLASS, r"^\s*(?:class|module)\s+(?P<name>[\w:]+)")],
        [r"""^\s*require(?:_relative)?\s+['"](?P<target>[^'"]+)['"]"""],
    ),
    "php": _spec(
        "php",
        [(NodeKind.FUNCTION, r"^\s*(?:public|private|protected|static|abstract|final|\s)*\bfunction\s+&?(?P<name>\w+)"),
         (NodeKind.CLASS, r"^\s*(?:abstract\s+|final\s+)?(?:class|interface|trait|enum)\s+(?P<name>\w+)")],
        [r"^\s*use\s+(?P<target>[\w\\]+)",
         r"""^\s*(?:require|include)(?:_once)?\s*\(?['"](?P<target>[^'"]+)['"]"""],
    ),
    "swift": _spec(
        "swift",
        [(NodeKind.FUNCTION, r"^\s*(?:public|private|internal|open|static|override|\s)*\bfunc\s+(?P<name>\w+)"),
         (NodeKind.CLASS,
          r"^\s*(?:public|private|internal|open|final|\s)*\b(?:class|struct|enum|protocol|actor)\s+(?P<name>\w+)")],
        [r"^\s*import\s+(?P<target>\w+)"],
    ),
    "shell": _spec(
        "shell",
        [(NodeKind.FUNCTION, r"^\s*(?:function\s+)?(?P<name>[A-Za-z_][\w-]*)\s*\(\)\s*\{?")],
        [r"^\s*(?:source|\.)\s+(?P<target>\S+)"],
    ),
}

EXTENSIONS: dict[str, str] = {
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".tsx": "typescript",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".kt": "kotlin", ".kts": "kotlin",
    ".cs": "csharp",
    ".c": "c", ".h": "c",
    ".cpp": "cpp", ".cc": "cpp", ".cxx": "cpp", ".hpp": "cpp", ".hh": "cpp",
    ".rb": "ruby",
    ".php": "php",
    ".swift": "swift",
    ".sh": "shell", ".bash": "shell", ".zsh": "shell",
}


def language_for(path: str) -> str | None:
    """Supported language for a path by extension, or None."""
    dot = path.rfind(".")
    if dot < 0:
        return None
    return EXTENSIONS.get(path[dot:].lower())


def _scan_defs(lines: list[str], spec: LangSpec) -> list[tuple[str, str, int]]:
    """(kind, name, lineno) for each top-level-ish definition, in file order."""
    defs: list[tuple[str, str, int]] = []
    seen: set[str] = set()
    for i, line in enumerate(lines, 1):
        for kind, rx in spec.defs:
            m = rx.match(line)
            if m:
                name = m.group("name")
                if name and name not in seen:
                    seen.add(name)  # keep the first definition of a name
                    defs.append((kind, name, i))
                break
    return defs


def _scan_imports(lines: list[str], spec: LangSpec) -> list[tuple[str, int]]:
    targets: list[tuple[str, int]] = []
    in_block = False
    for i, line in enumerate(lines, 1):
        if spec.import_block is not None:
            open_rx, item_rx, close_rx = spec.import_block
            if in_block:
                if close_rx.match(line):
                    in_block = False
                    continue
                m = item_rx.match(line)
                if m:
                    targets.append((m.group("target"), i))
                continue
            if open_rx.match(line):
                in_block = True
                continue
        for rx in spec.imports:
            m = rx.search(line)
            if m:
                targets.append((m.group("target"), i))
                break
    return targets


def _def_spans(defs: list[tuple[str, str, int]], total_lines: int) -> list[int]:
    """end_lineno per definition: up to the line before the next def (capped)."""
    ends: list[int] = []
    for idx, (_, _, lineno) in enumerate(defs):
        nxt = defs[idx + 1][2] - 1 if idx + 1 < len(defs) else total_lines
        ends.append(min(max(nxt, lineno), lineno + _MAX_DEF_LINES))
    return ends


def slice_hash_for(lines: list[str], lineno: int, end_lineno: int) -> str:
    """Content hash of a definition's line span — the invalidation unit."""
    return file_content_hash("\n".join(lines[lineno - 1:end_lineno]))


def extract(path: str, content: str, language: str) -> tuple[list[Node], list[Edge]]:
    """Regex pass over one non-Python file: module + top-level defs + imports.
    Mirrors :func:`.substrate.extract`'s shape so the indexer can dispatch."""
    spec = SPECS[language]
    chash = file_content_hash(content)
    lines = content.splitlines()
    module = module_of(path)
    mid = node_id(path)
    nodes = [Node(id=mid, kind=NodeKind.MODULE, name=module or path, path=path,
                  lineno=1, end_lineno=len(lines), module=module,
                  content_hash=chash, slice_hash=chash)]
    edges: list[Edge] = []

    defs = _scan_defs(lines, spec)
    ends = _def_spans(defs, len(lines))
    for (kind, name, lineno), end in zip(defs, ends):
        nid = node_id(path, name)
        nodes.append(Node(
            id=nid, kind=kind, name=name, path=path, lineno=lineno,
            end_lineno=end, module=module, content_hash=chash,
            slice_hash=slice_hash_for(lines, lineno, end)))
        edges.append(Edge(src=mid, dst=nid, kind=EdgeKind.CONTAINS,
                          path=path, lineno=lineno, resolved=True))

    for target, lineno in _scan_imports(lines, spec):
        edges.append(Edge(src=mid, dst=target, kind=EdgeKind.IMPORTS,
                          path=path, lineno=lineno, resolved=False))
    return nodes, edges


def extract_symbol_source(content: str, symbol: str, language: str) -> tuple[str, int, int] | None:
    """Best-effort slice for ``symbol`` — the counterpart of
    :func:`.substrate.extract_symbol_source` for generic languages, used by
    citation refetch. Matches the last path segment of dotted symbols."""
    spec = SPECS.get(language)
    if spec is None:
        return None
    want = symbol.split(".")[-1]
    lines = content.splitlines()
    defs = _scan_defs(lines, spec)
    ends = _def_spans(defs, len(lines))
    for (kind, name, lineno), end in zip(defs, ends):
        if name == want:
            return "\n".join(lines[lineno - 1:end]), lineno, end
    return None
