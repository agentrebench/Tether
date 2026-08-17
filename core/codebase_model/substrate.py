"""Derived substrate — the parser-produced, regenerable ground truth.

Turns a Python source file into :class:`Node` and :class:`Edge` rows: modules,
classes, functions/methods, and the ``contains`` / ``imports`` / ``inherits`` /
``calls`` edges between them, plus a best-effort ``writes`` signal for the data
pillar. No confidence — this is a materialized view of HEAD, re-runnable and
``O(Δ)`` to refresh (see 01-architecture-split.md).

Name binding is intentionally light: ``calls``/``inherits`` edges store the
callee's *simple name* as ``dst`` (``resolved=False``). Reverse reachability for
blast-radius then matches on name — best-effort, as the spec allows. The richer
resolution pass lives in :mod:`.indexer`.

This module is stdlib-only (``ast``, ``hashlib``) and never imports the rest of
the package, so :mod:`.citations` can borrow :func:`extract_symbol_source`
without a cycle.
"""
from __future__ import annotations

import ast
import hashlib

from .model import Edge, EdgeKind, Node, NodeKind, module_of, node_id

# Best-effort persistence/IO sinks for the data pillar. A call to a method with
# one of these names is recorded as a WRITES edge so invariants like
# "all writes go through UnitOfWork" have something deterministic to check.
_WRITE_SINKS = {"save", "commit", "execute", "write", "insert", "update",
                "delete", "flush", "create", "bulk_create", "executemany"}


def file_content_hash(text: str) -> str:
    """Stable content hash of a file's text (sha1 hex). Drives change detection
    and commit-independent caching."""
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()


def extract_symbol_source(content: str, qualname: str) -> tuple[str, int, int] | None:
    """Return ``(source, lineno, end_lineno)`` for the def named ``qualname``
    (dotted for methods, e.g. ``RefundService.refund``), or ``None`` if absent.
    Used by :func:`.citations.refetch` to pull exactly one cited slice."""
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return None
    target = qualname.split(".")
    found = _find_def(tree, target)
    if found is None:
        return None
    seg = ast.get_source_segment(content, found)
    if seg is None:
        # Fallback: slice by line numbers.
        lines = content.splitlines()
        start = getattr(found, "lineno", 1)
        end = getattr(found, "end_lineno", start)
        seg = "\n".join(lines[start - 1:end])
    return seg, getattr(found, "lineno", 1), getattr(found, "end_lineno", 0) or 0


def _find_def(scope: ast.AST, path: list[str]):
    """Walk nested ClassDef/FunctionDef matching ``path`` segment by segment."""
    if not path:
        return scope if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) else None
    head, rest = path[0], path[1:]
    for child in getattr(scope, "body", []):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) \
                and child.name == head:
            if not rest:
                return child
            return _find_def(child, rest)
    return None


def _callee_name(call: ast.Call) -> str | None:
    """Simple name of a call target: ``foo(...)`` -> ``foo``; ``a.b.foo(...)`` -> ``foo``."""
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


class _Extractor(ast.NodeVisitor):
    def __init__(self, path: str, content_hash: str, lines: list[str] | None = None):
        self.path = path
        self.module = module_of(path)
        self.content_hash = content_hash
        self.module_id = node_id(path)
        self._lines = lines or []
        self.nodes: list[Node] = [
            Node(id=self.module_id, kind=NodeKind.MODULE, name=self.module or path,
                 path=path, lineno=1, end_lineno=0, module=self.module,
                 content_hash=content_hash, slice_hash=content_hash)
        ]
        self.edges: list[Edge] = []
        # scope stack of (qualname, node_id); seed with module
        self._scope: list[tuple[str, str]] = [("", self.module_id)]

    # -- helpers -----------------------------------------------------------
    @property
    def _scope_id(self) -> str:
        return self._scope[-1][1]

    @property
    def _scope_qual(self) -> str:
        return self._scope[-1][0]

    def _qual(self, name: str) -> str:
        q = self._scope_qual
        return f"{q}.{name}" if q else name

    def _add_def(self, node: ast.AST, name: str, kind: str) -> str:
        qual = self._qual(name)
        nid = node_id(self.path, qual)
        lineno = getattr(node, "lineno", 0)
        end_lineno = getattr(node, "end_lineno", 0) or 0
        # Per-symbol slice hash: an edit elsewhere in the file leaves this
        # unchanged, so beliefs citing the symbol survive the reindex.
        slice_hash = ""
        if self._lines and lineno:
            slice_hash = file_content_hash(
                "\n".join(self._lines[lineno - 1:end_lineno or lineno]))
        self.nodes.append(Node(
            id=nid, kind=kind, name=qual, path=self.path,
            lineno=lineno, end_lineno=end_lineno,
            module=self.module, content_hash=self.content_hash,
            slice_hash=slice_hash))
        self.edges.append(Edge(src=self._scope_id, dst=nid, kind=EdgeKind.CONTAINS,
                               path=self.path, lineno=getattr(node, "lineno", 0),
                               resolved=True))
        return nid

    # -- visitors ----------------------------------------------------------
    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        nid = self._add_def(node, node.name, NodeKind.CLASS)
        for base in node.bases:
            bname = base.id if isinstance(base, ast.Name) else (
                base.attr if isinstance(base, ast.Attribute) else None)
            if bname:
                self.edges.append(Edge(src=nid, dst=bname, kind=EdgeKind.INHERITS,
                                       path=self.path, lineno=node.lineno, resolved=False))
        self._scope.append((self._qual(node.name), nid))
        self.generic_visit(node)
        self._scope.pop()

    def _visit_func(self, node) -> None:
        parent_kind = NodeKind.METHOD if self.nodes and self._is_in_class() else NodeKind.FUNCTION
        nid = self._add_def(node, node.name, parent_kind)
        self._scope.append((self._qual(node.name), nid))
        self.generic_visit(node)
        self._scope.pop()

    visit_FunctionDef = _visit_func
    visit_AsyncFunctionDef = _visit_func

    def _is_in_class(self) -> bool:
        # the current scope's node is a class if the most recent pushed scope id
        # belongs to a class node we just created
        cur = self._scope_id
        for n in self.nodes:
            if n.id == cur:
                return n.kind == NodeKind.CLASS
        return False

    def visit_Call(self, node: ast.Call) -> None:
        name = _callee_name(node)
        if name:
            self.edges.append(Edge(src=self._scope_id, dst=name, kind=EdgeKind.CALLS,
                                   path=self.path, lineno=getattr(node, "lineno", 0),
                                   resolved=False))
            if isinstance(node.func, ast.Attribute) and node.func.attr in _WRITE_SINKS:
                self.edges.append(Edge(src=self._scope_id, dst=f"sink:{node.func.attr}",
                                       kind=EdgeKind.WRITES, path=self.path,
                                       lineno=getattr(node, "lineno", 0), resolved=False))
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.edges.append(Edge(src=self.module_id, dst=alias.name, kind=EdgeKind.IMPORTS,
                                   path=self.path, lineno=node.lineno, resolved=False))

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        base = node.module or ""
        for alias in node.names:
            target = f"{base}.{alias.name}" if base else alias.name
            self.edges.append(Edge(src=self.module_id, dst=target, kind=EdgeKind.IMPORTS,
                                   path=self.path, lineno=node.lineno, resolved=False))


def extract(path: str, content: str) -> tuple[list[Node], list[Edge]]:
    """Parse one Python file (``path`` is its repo-relative posix path) into
    nodes and edges. On a syntax error, returns just the module node (so the file
    is still tracked) and no edges."""
    chash = file_content_hash(content)
    try:
        tree = ast.parse(content)
    except SyntaxError:
        mid = node_id(path)
        return [Node(id=mid, kind=NodeKind.MODULE, name=module_of(path) or path,
                     path=path, content_hash=chash, module=module_of(path),
                     slice_hash=chash)], []
    ex = _Extractor(path, chash, lines=content.splitlines())
    ex.visit(tree)
    return ex.nodes, ex.edges
