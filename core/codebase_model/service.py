"""The service facade — wires the five pieces into one ``CodebaseModel``.

Everything below this layer is a single-purpose component (substrate store,
``O(Δ)`` indexer, belief cache, invariant engine, query surface). This module is
the seam the rest of the harness talks to: it owns their composition, the
per-repo singleton, the build/sync lifecycle, and the edit-hook entry point.

Two lifecycle invariants matter here:

  * ``teardown_substrate`` drops only the *derived* layer (nodes/edges/files);
    beliefs, invariants and decisions survive — that's the whole bet, that the
    learned cache outlives the regenerable substrate (see the acceptance test in
    ``docs/codebase-mental-model/11-acceptance-test.md``).
  * ``on_edit`` is called from a hook in the edit path, so it must **never**
    raise into the engine — it wraps everything and swallows failures.

Stdlib-only. Depends on the foundation modules + :mod:`..config` for the model
db location. The module-level :func:`get_model` cache mirrors
``core/skills.default_store``: one live instance per resolved repo root.
"""
from __future__ import annotations

import contextlib
import hashlib
import os
import subprocess
from pathlib import Path

from ..config import CONFIG_DIR
from .beliefs import BeliefManager
from .indexer import Indexer
from .invariants import InvariantEngine
from .query import QuerySurface
from .store import ModelStore


def model_db_path(repo_root: Path | str) -> Path:
    """Stable db location for a repo root: ``CONFIG_DIR/models/<sha1[:16]>.db``.

    Content-addressed on the *absolute* root path, so the same checkout always
    maps to the same db regardless of the cwd a query is issued from, and two
    distinct checkouts never collide."""
    abspath = os.path.abspath(str(repo_root))
    digest = hashlib.sha1(abspath.encode("utf-8")).hexdigest()[:16]
    return CONFIG_DIR / "models" / f"{digest}.db"


def _git_toplevel(cwd: str) -> str | None:
    """The git work-tree root containing ``cwd``, or None when there's no repo /
    git is unavailable. Used to resolve the canonical root for the cache key."""
    try:
        out = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def _resolve_root(cwd: Path | str | None) -> Path:
    """Resolve ``cwd`` to a repo root: the enclosing git toplevel when there is
    one, else the directory itself (so the model works in a bare tree too)."""
    base = os.path.abspath(str(cwd) if cwd is not None else os.getcwd())
    top = _git_toplevel(base)
    return Path(top) if top else Path(base)


class CodebaseModel:
    """The composed model. Owns the store and the four components built over it,
    and exposes the lifecycle + the read/record surface the agent uses."""

    def __init__(self, repo_root: Path | str, db_path: Path | str | None = None,
                 *, max_beliefs: int = 500, verifier=None):
        self.repo_root = Path(repo_root)
        self.db_path = Path(db_path) if db_path is not None else model_db_path(repo_root)
        # one store, shared by every component (single source of truth)
        self.store = ModelStore(self.db_path)
        self.indexer = Indexer(self.store, self.repo_root)
        self.beliefs = BeliefManager(self.store, max_beliefs=max_beliefs, verifier=verifier)
        self.invariants = InvariantEngine(self.store)
        self.query = QuerySurface(self.store, self.indexer, self.beliefs, self.invariants)

    # -- lifecycle ---------------------------------------------------------
    def build(self, on_progress=None) -> dict:
        """Cold ``O(n)`` build: parse the whole repo into the substrate and stamp
        the current commit. Returns the indexer's ``{"indexed", "files"}``.
        ``on_progress(done, total, rel_path)`` is forwarded to the indexer for a
        progress display."""
        with self.store.exclusive():
            result = self.indexer.cold_build(on_progress=on_progress)
            self.store.set_meta("commit", self.indexer.current_commit())
            self.store.set_meta("root", str(self.repo_root))
        return result

    def sync(self) -> dict:
        """Steady-state ``O(Δ)`` refresh: diff the working tree against the store,
        reindex/remove the delta, mark beliefs citing changed-or-removed files
        stale (cheap invalidation — re-verification stays lazy), restamp commit.
        Returns ``{"indexed", "removed", "invalidated"}``."""
        with self.store.exclusive():
            changed, removed = self.indexer.changed_since_index()
            # Snapshot per-symbol slice hashes BEFORE the reindex overwrites
            # them — the diff against the fresh hashes is what makes
            # invalidation symbol-granular instead of file-granular.
            pre = {p: self.store.node_slice_hashes(p) for p in changed}
            commit = self.indexer.current_commit()
            result = self.indexer.update(changed, removed, commit=commit)
            invalidated = set(self.beliefs.invalidate(removed))
            for path in changed:
                invalidated.update(self._invalidate_changed_symbols(path, pre.get(path, {})))
            self.store.set_meta("commit", commit)
            self.store.set_meta("root", str(self.repo_root))
        result["invalidated"] = len(invalidated)
        return result

    def _invalidate_changed_symbols(self, path: str, old: dict[str, str]) -> list[str]:
        """Diff a file's per-symbol slice hashes across a reindex and stale-mark
        only the beliefs whose cited symbol moved. Pre-migration rows carry
        empty hashes, so every symbol reads as changed — a safe, conservative
        fallback to the old file-granular behavior."""
        new = self.store.node_slice_hashes(path)
        changed_syms = {n for n, h in old.items() if new.get(n, "") != h}
        changed_syms |= {n for n in new if n not in old}
        return self.beliefs.invalidate_symbols(path, changed_syms)

    def sync_if_commit_changed(self) -> dict | None:
        """Cheap guard against mid-session VCS movement (pull / checkout /
        commit in another terminal). One ``git rev-parse`` when clean; a full
        :meth:`sync` only when HEAD no longer matches the stamped commit.
        Returns the sync result, or None when nothing moved (or on failure —
        staleness is recoverable, so this never raises)."""
        try:
            current = self.indexer.current_commit()
            stored = self.store.get_meta("commit")
            # `not stored` means never built — that's the startup path's job,
            # not a per-turn implicit full reindex.
            if not current or not stored or current == stored:
                return None
            return self.sync()
        except Exception:
            return None

    def on_edit(self, rel_path: str) -> None:
        """Edit-hook entry point: reindex the one touched file and invalidate any
        belief citing it. Called from the edit path, so it must never raise — all
        failures are swallowed (a stale substrate is recoverable; a crashed edit
        is not)."""
        try:
            # Short lock timeout: if another session is mid-reindex, skip —
            # a stale substrate is recoverable at the next sync, and the edit
            # hook must never stall the user's edit.
            with self.store.exclusive(timeout=5.0):
                pre = self.store.node_slice_hashes(rel_path)
                if self.indexer.index_file(rel_path):
                    self._invalidate_changed_symbols(rel_path, pre)
        except Exception:
            # a model hiccup must not break the user's edit
            pass

    def teardown_substrate(self) -> None:
        """Drop the entire derived layer (nodes/edges/files) while leaving the
        inferred layer (beliefs/invariants/decisions) intact. ``delete_file``
        cascades nodes+edges, so iterating the file rows clears the substrate.
        This is the acceptance-test pivot: prove the model still answers from the
        learned cache after its ground truth is gone."""
        for path in list(self.store.all_files()):
            self.store.delete_file(path)

    # -- read surface (delegates to the query layer) ----------------------
    def affects(self, target: str) -> dict:
        return self.query.affects(target)

    def owns(self, topic: str) -> dict:
        return self.query.owns(topic)

    def allowed(self, description: str, changed_files: list[str] | None = None) -> dict:
        return self.query.allowed(description, changed_files)

    def architecture_index(self) -> str:
        return self.query.architecture_index()

    def answer(self, question: str) -> str:
        return self.query.answer(question)

    def consult(self, belief_id: str):
        """Consult a belief (LRU touch) and, if it's stale, lazily re-verify it
        against the repo *now* — the one place the expensive refetch+verify runs,
        and only because something actually wanted the belief. A belief whose
        citations no longer resolve is demoted (and may be evicted); a verified
        one is reinforced. Returns the (possibly updated) belief, or None."""
        belief = self.beliefs.consult(belief_id)
        if belief is not None and belief.stale:
            self.beliefs.reverify(belief_id, self.repo_root)
            belief = self.beliefs.get(belief_id)
        return belief

    # -- record surface (delegates to the managers) -----------------------
    def record_belief(self, claim: str, **kwargs):
        """Capture (or supersede) a descriptive belief. See ``BeliefManager.add``."""
        return self.beliefs.add(claim, **kwargs)

    def record_invariant(self, claim: str, **kwargs):
        """Capture a prescriptive invariant. See ``InvariantEngine.add``."""
        return self.invariants.add(claim, **kwargs)

    def record_decision(self, *, reason: str, **kwargs):
        """Capture a decision (typically a rejected pattern). See
        ``InvariantEngine.add_decision``."""
        return self.invariants.add_decision(reason=reason, **kwargs)

    # -- cleanup -----------------------------------------------------------
    def close(self) -> None:
        self.store.close()


# Process-wide cache: one CodebaseModel per resolved repo root (mirrors
# core/skills.default_store). Keyed by the canonical root path so queries issued
# from any subdirectory share the same live instance and its single db handle.
_MODELS: dict[str, CodebaseModel] = {}

# Verifier applied to every model this process serves. The REPL installs an
# LLM-backed one (verify.llm_verifier) once its backend exists; until then
# reverify runs in trust-on-refetch mode, same as before.
_DEFAULT_VERIFIER = None


def set_default_verifier(verifier) -> None:
    """Install the verifier on all cached models and any built later."""
    global _DEFAULT_VERIFIER
    _DEFAULT_VERIFIER = verifier
    for model in _MODELS.values():
        model.beliefs.verifier = verifier


def gc_models(*, include_home: bool = True) -> dict:
    """Sweep ``~/.tether/models``: delete model DBs whose recorded repo root
    no longer exists (the repo was moved or deleted — the path-hashed key means
    it will never be opened again) and, by default, legacy home-directory
    models (never opened since the home guard, and typically huge).

    DBs predating the ``root`` meta stamp can't be attributed to a repo, so
    they're left alone and reported. Returns
    ``{"removed": [{db, root, bytes}], "kept": [...], "unknown": [...]}``.
    """
    import sqlite3 as _sq

    models_dir = CONFIG_DIR / "models"
    report = {"removed": [], "kept": [], "unknown": []}
    if not models_dir.is_dir():
        return report
    home = Path(os.path.expanduser("~"))
    home_db_name = model_db_path(home).name

    for db in sorted(models_dir.glob("*.db")):
        root = None
        try:
            with contextlib.closing(_sq.connect(f"file:{db}?mode=ro", uri=True)) as conn:
                row = conn.execute("SELECT value FROM meta WHERE key='root'").fetchone()
            root = row[0] if row else None
        except _sq.Error:
            pass  # unreadable/corrupt — treat as unattributable

        is_home = db.name == home_db_name or (root is not None and Path(root) == home)
        orphaned = root is not None and not Path(root).is_dir()
        if orphaned or (include_home and is_home):
            size = db.stat().st_size if db.exists() else 0
            for suffix in ("", "-wal", "-shm"):
                Path(str(db) + suffix).unlink(missing_ok=True)
            db.with_suffix(".lock").unlink(missing_ok=True)
            report["removed"].append({
                "db": db.name,
                "root": root or ("(home directory)" if is_home else "(unknown)"),
                "bytes": size,
            })
        elif root is None:
            report["unknown"].append(db.name)
        else:
            report["kept"].append(db.name)

    # Dangling companions: -wal/-shm/.lock files whose parent .db is gone
    # (left behind by an interrupted session or an out-of-band delete).
    for stray in sorted(models_dir.iterdir()):
        base = stray.name
        for suffix in (".db-wal", ".db-shm", ".lock"):
            if base.endswith(suffix):
                parent = models_dir / (base[: -len(suffix)] + ".db")
                if not parent.exists():
                    stray.unlink(missing_ok=True)
                    report["removed"].append(
                        {"db": base, "root": "(dangling companion file)", "bytes": 0})
                break
    return report


def get_model(cwd: Path | str | None = None) -> CodebaseModel:
    """The shared model for the repo containing ``cwd`` (defaults to the process
    cwd). Resolves to the git toplevel when there is one so every subdirectory
    maps to the same instance; caches and reuses it for the process lifetime."""
    root = _resolve_root(cwd)
    key = str(root)
    model = _MODELS.get(key)
    if model is None:
        model = CodebaseModel(root, verifier=_DEFAULT_VERIFIER)
        _MODELS[key] = model
    return model
