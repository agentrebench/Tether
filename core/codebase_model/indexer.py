"""Substrate indexer — the O(Δ) update loop over the derived layer.

The indexer is the *eager, cheap* half of the two-loop design (see
``docs/codebase-mental-model/07-complexity-targets.md``): it keeps the SQLite
substrate a materialized view of HEAD by parsing only what changed. It owns no
opinions — it just drives :mod:`.substrate` extraction into :mod:`.store` and
answers reverse-reachability ("blast radius") queries off the stored call graph.

Cost contracts it must honour:

  * cold build — ``O(n)`` once, parse the whole repo;
  * steady-state update — ``O(Δ)``, reindex only changed files (an unchanged
    file is skipped by content-hash, so a no-op edit costs one hash);
  * blast radius — walks the call graph, so its cost is the true downstream
    impact, which the graph can quote before you pay it.

Git is consulted only to stamp the current commit onto indexed files; every git
call degrades to ``""`` when git is missing or errors, so the indexer works in a
bare directory with no repository.

Depends only on :mod:`.store` and :mod:`.substrate`.
"""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path, PurePosixPath

from . import substrate
from .model import EdgeKind, Node
from .store import ModelStore

# Directories never worth parsing. Pruned during discovery; ``*.egg-info`` is
# matched by suffix below since the package name varies.
_SKIP_DIRS = {".git", "__pycache__", "venv", ".venv", "node_modules"}


class Indexer:
    """Drives substrate extraction into the store and answers reachability."""

    def __init__(self, store: ModelStore, repo_root: Path | str,
                 max_files: int = 10000):
        self.store = store
        self.repo_root = Path(repo_root)
        # Substrate bound: nodes/edges derive from files, so capping discovery
        # caps the whole derived layer on pathological trees.
        self.max_files = max_files
        self.last_skipped = 0  # files dropped by the cap on the last discovery

    # -- git ---------------------------------------------------------------
    def current_commit(self) -> str:
        """Short HEAD hash, or ``""`` when there's no git / any error. Stamped
        onto file rows so the substrate is commit-keyed (resumable cold build)."""
        try:
            out = subprocess.run(
                ["git", "-C", str(self.repo_root), "rev-parse", "--short", "HEAD"],
                capture_output=True, text=True, timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            return ""
        return out.stdout.strip() if out.returncode == 0 else ""

    # -- discovery ---------------------------------------------------------
    def discover_source_files(self) -> list[str]:
        """Every repo-relative posix source file under the root — ``*.py`` plus
        the generic-extractor languages (js/ts, go, rust, java, c/c++, ruby, …)
        — skipping vcs/build dirs and the model db itself. Bounded by
        ``max_files`` (Python first, then by path) so a pathological tree can't
        produce an unbounded substrate; the overflow count lands in
        ``last_skipped``."""
        from .substrate_generic import language_for

        db_path = str(self.store.path)
        found: list[str] = []
        root = str(self.repo_root)
        for dirpath, dirnames, filenames in os.walk(root):
            # prune in place so os.walk doesn't descend into skipped trees
            dirnames[:] = [
                d for d in dirnames
                if d not in _SKIP_DIRS and not d.endswith(".egg-info")
            ]
            for fn in filenames:
                if not fn.endswith(".py") and language_for(fn) is None:
                    continue
                full = Path(dirpath) / fn
                if str(full) == db_path:
                    continue
                rel = os.path.relpath(str(full), root)
                found.append(PurePosixPath(Path(rel)).as_posix())
        found.sort()
        self.last_skipped = 0
        if len(found) > self.max_files:
            # Python keeps priority (it has the richest extraction), then
            # alphabetical — deterministic, so sync sees the same subset.
            found.sort(key=lambda p: (not p.endswith(".py"), p))
            self.last_skipped = len(found) - self.max_files
            found = sorted(found[:self.max_files])
        return found

    # Backwards-compatible name (predates multi-language support).
    discover_python_files = discover_source_files

    # -- single-file indexing ---------------------------------------------
    def index_file(self, rel_path: str, content: str | None = None, commit: str = "") -> bool:
        """(Re)index one file. Reads it from disk when ``content`` is None.

        Skips and returns False when the stored content-hash already matches —
        this is what makes a no-op edit cost only a hash, keeping steady-state
        updates ``O(Δ)``. Otherwise it drops the file's old nodes/edges, extracts
        fresh ones, upserts, and stamps the file row. Returns True iff indexed."""
        if content is None:
            try:
                content = (self.repo_root / rel_path).read_text(errors="replace")
            except OSError:
                return False
        chash = substrate.file_content_hash(content)
        if self.store.get_file_hash(rel_path) == chash:
            return False  # unchanged — nothing to do
        # Replace the changed slice: clear then re-derive.
        self.store.delete_edges_in_file(rel_path)
        self.store.delete_nodes_in_file(rel_path)
        nodes, edges = self._extract(rel_path, content)
        self.store.upsert_nodes(nodes)
        self.store.insert_edges(edges)
        self.store.set_file(rel_path, chash, commit, time.time())
        return True

    @staticmethod
    def _extract(rel_path: str, content: str):
        """Dispatch: Python gets the AST extractor, everything else the
        regex-based generic one."""
        if rel_path.endswith(".py"):
            return substrate.extract(rel_path, content)
        from . import substrate_generic
        lang = substrate_generic.language_for(rel_path)
        if lang is not None:
            return substrate_generic.extract(rel_path, content, lang)
        return substrate.extract(rel_path, content)

    def remove_file(self, rel_path: str) -> None:
        """Drop a file and everything the substrate derived from it."""
        self.store.delete_file(rel_path)

    # -- bulk indexing -----------------------------------------------------
    def cold_build(self, paths: list[str] | None = None, on_progress=None) -> dict:
        """One-time ``O(n)`` parse of the whole repo (or a given file list).
        Returns ``{"indexed": n, "files": total}`` (``indexed`` counts files that
        actually changed, so a rebuild over a current store reports zero).

        ``on_progress(done, total, rel_path)`` is invoked after each file when
        given, so a caller can render progress. It stays UI-agnostic — any
        exception from the callback is swallowed so a broken renderer can't fail
        the build."""
        paths = self.discover_source_files() if paths is None else list(paths)
        commit = self.current_commit()
        total = len(paths)
        indexed = 0
        for i, p in enumerate(paths, 1):
            if self.index_file(p, commit=commit):
                indexed += 1
            if on_progress is not None:
                try:
                    on_progress(i, total, p)
                except Exception:
                    pass
        return {"indexed": indexed, "files": total, "skipped": self.last_skipped}

    def update(self, changed: list[str], removed: list[str] | None = None,
               commit: str = "") -> dict:
        """Steady-state ``O(Δ)`` update: reindex ``changed``, drop ``removed``.
        Returns ``{"indexed": n, "removed": m}``."""
        removed = list(removed or [])
        if not commit:
            commit = self.current_commit()
        indexed = sum(1 for p in changed if self.index_file(p, commit=commit))
        for p in removed:
            self.remove_file(p)
        return {"indexed": indexed, "removed": len(removed)}

    def changed_since_index(self) -> tuple[list[str], list[str]]:
        """Diff the working tree against the store by content-hash. Returns
        ``(changed, removed)``: files whose disk hash differs from the stored one
        (new files included), and files in the store no longer on disk."""
        disk = self.discover_source_files()
        disk_set = set(disk)
        changed: list[str] = []
        for p in disk:
            try:
                content = (self.repo_root / p).read_text(errors="replace")
            except OSError:
                continue
            if self.store.get_file_hash(p) != substrate.file_content_hash(content):
                changed.append(p)
        removed = [p for p in self.store.all_files() if p not in disk_set]
        removed.sort()
        return changed, removed

    # -- reachability ------------------------------------------------------
    def blast_radius(self, target: str, max_depth: int = 6) -> list[str]:
        """Reverse-dependency closure of ``target`` as a list of caller node ids.

        ``target`` may be a node id or a symbol name (resolved via
        ``find_nodes_by_name``). We BFS the call graph backwards: a node's callers
        are the ``src`` of CALLS edges whose ``dst`` is the node's name. Because
        the substrate stores the callee's *simple* name in ``dst`` (see
        :mod:`.substrate`), binding is by simple name — best-effort: two distinct
        symbols sharing a simple name will conflate, which is acceptable for an
        impact estimate that errs toward over-inclusion. The target's own nodes
        are excluded; results are deduped and capped at ``max_depth`` hops."""
        seeds = self._resolve(target)
        seen = {n.id for n in seeds}  # never re-add (also excludes the target)
        result: list[str] = []
        frontier: list[tuple[Node, int]] = [(n, 0) for n in seeds]
        while frontier:
            node, depth = frontier.pop(0)
            if depth >= max_depth:
                continue
            for caller_id in self._callers_of(node):
                if caller_id in seen:
                    continue
                seen.add(caller_id)
                result.append(caller_id)
                caller = self.store.get_node(caller_id)
                if caller is not None:
                    frontier.append((caller, depth + 1))
        return result

    def affected_files(self, rel_path: str, max_depth: int = 6) -> list[str]:
        """Sorted unique paths of every node in the blast radius of every symbol
        defined in ``rel_path`` — i.e. which files an edit to this one can reach."""
        paths: set[str] = set()
        for node in self.store.nodes_in_file(rel_path):
            for nid in self.blast_radius(node.id, max_depth=max_depth):
                hit = self.store.get_node(nid)
                if hit is not None and hit.path:
                    paths.add(hit.path)
        return sorted(paths)

    # -- internals ---------------------------------------------------------
    def _resolve(self, target: str) -> list[Node]:
        """Resolve ``target`` to seed nodes: an exact node id first, else a name."""
        node = self.store.get_node(target)
        if node is not None:
            return [node]
        return self.store.find_nodes_by_name(target)

    def _callers_of(self, node: Node) -> set[str]:
        """Caller node ids for ``node`` via CALLS edges keyed on its simple name.
        We also try the full qualname so top-level functions (whose name == simple
        name) and any future resolved edges are covered."""
        simple = node.name.split(".")[-1]
        callers: set[str] = set()
        for name in {node.name, simple}:
            if not name:
                continue
            for edge in self.store.edges_to(name, kind=EdgeKind.CALLS):
                callers.add(edge.src)
        return callers
