"""SQLite store — the single source of truth.

Everything durable is an indexed row here: substrate nodes/edges, file content
hashes, inferred beliefs/invariants/decisions, content-addressed evidence
pointers, and materialized invariant-check results. B-tree indexes give the
``O(log n + k)`` lookups that make total store size irrelevant to context
footprint (see 06-storage-runtime.md, 07-complexity-targets.md).

Concurrency model (from the spec): WAL mode → concurrent readers + a single
writer. The owning process serialises its own writes with a lock; the schema is
designed so a future external indexer daemon can be the sole writer while agents
read. ``sqlite3`` is stdlib, so this introduces no new dependency.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import sqlite3
import threading
import time
from pathlib import Path

from .model import Belief, Decision, Edge, Invariant, Node

SCHEMA_VERSION = "1"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS files (
    path         TEXT PRIMARY KEY,
    content_hash TEXT,
    commit_hash  TEXT,
    indexed_at   REAL
);
CREATE TABLE IF NOT EXISTS nodes (
    id           TEXT PRIMARY KEY,
    kind         TEXT,
    name         TEXT,
    path         TEXT,
    lineno       INTEGER,
    end_lineno   INTEGER,
    module       TEXT,
    content_hash TEXT,
    slice_hash   TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_nodes_path   ON nodes(path);
CREATE INDEX IF NOT EXISTS idx_nodes_module ON nodes(module);
CREATE INDEX IF NOT EXISTS idx_nodes_name   ON nodes(name);

CREATE TABLE IF NOT EXISTS edges (
    src      TEXT,
    dst      TEXT,
    kind     TEXT,
    path     TEXT,
    lineno   INTEGER,
    resolved INTEGER,
    PRIMARY KEY (src, dst, kind, path, lineno)
);
CREATE INDEX IF NOT EXISTS idx_edges_src  ON edges(src);
CREATE INDEX IF NOT EXISTS idx_edges_dst  ON edges(dst);
CREATE INDEX IF NOT EXISTS idx_edges_kind ON edges(kind);
CREATE INDEX IF NOT EXISTS idx_edges_path ON edges(path);

CREATE TABLE IF NOT EXISTS beliefs (
    id            TEXT PRIMARY KEY,
    claim         TEXT,
    confidence    REAL,
    justified_by  TEXT,   -- JSON list of raw citation strings
    kind          TEXT,
    on_conflict   TEXT,
    verified      TEXT,
    confirmations INTEGER,
    last_consulted REAL,
    stale         INTEGER,
    created       REAL,
    source        TEXT
);
CREATE INDEX IF NOT EXISTS idx_beliefs_consulted ON beliefs(last_consulted);

-- content-addressed evidence pointers, decomposed for O(log n) invalidation
CREATE TABLE IF NOT EXISTS evidence (
    belief_id   TEXT,
    file        TEXT,
    symbol      TEXT,
    commit_hash TEXT,
    raw         TEXT
);
CREATE INDEX IF NOT EXISTS idx_evidence_file   ON evidence(file);
CREATE INDEX IF NOT EXISTS idx_evidence_belief ON evidence(belief_id);

CREATE TABLE IF NOT EXISTS invariants (
    id           TEXT PRIMARY KEY,
    claim        TEXT,
    check_sexpr  TEXT,
    enforcement  TEXT,
    confidence   TEXT,   -- "confirmed" or a float as text
    on_conflict  TEXT,
    counterexample TEXT,
    source       TEXT,
    created      REAL
);

CREATE TABLE IF NOT EXISTS decisions (
    id              TEXT PRIMARY KEY,
    status          TEXT,
    detector        TEXT,
    accepted_pattern TEXT,
    reason          TEXT,
    created         REAL,
    archived        INTEGER
);

CREATE TABLE IF NOT EXISTS invariant_results (
    invariant_id  TEXT,
    passed        INTEGER,
    counterexample TEXT,
    run_at        REAL,
    commit_hash   TEXT,
    PRIMARY KEY (invariant_id, commit_hash)
);
"""


def _parse_citation_light(raw: str) -> tuple[str, str, str]:
    """Cheap split of ``file @ symbol @ commit`` for indexing. The authoritative
    parser is :func:`.citations.parse`; this only needs the file segment for the
    evidence index and tolerates missing pieces."""
    parts = [p.strip() for p in str(raw).split("@")]
    file = parts[0] if parts else ""
    symbol = parts[1] if len(parts) > 1 else ""
    commit = parts[2] if len(parts) > 2 else ""
    return file, symbol, commit


class ModelStore:
    def __init__(self, db_path: Path | str):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # Every REPL session is a writer (the spec's single-writer daemon does
        # not exist yet), so a second session's transaction must wait for the
        # first instead of surfacing "database is locked".
        self._conn.execute("PRAGMA busy_timeout=10000")
        # Negotiating WAL mode requires a write lock. Repeating that negotiation
        # for every GUI status query can stall behind another Tether session,
        # even though status itself is read-only. Only switch modes for a fresh
        # or legacy database; an existing WAL store stays entirely read-side.
        journal_mode = self._conn.execute("PRAGMA journal_mode").fetchone()[0]
        if str(journal_mode).lower() != "wal":
            self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA)
        self._migrate()
        if self.get_meta("schema_version") is None:
            self.set_meta("schema_version", SCHEMA_VERSION)
        self._conn.commit()

    def _migrate(self) -> None:
        """Additive migrations for DBs created before a column existed.
        CREATE TABLE IF NOT EXISTS won't add columns to an existing table."""
        cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(nodes)")}
        if "slice_hash" not in cols:
            self._conn.execute("ALTER TABLE nodes ADD COLUMN slice_hash TEXT DEFAULT ''")

    # -- lifecycle ---------------------------------------------------------
    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextlib.contextmanager
    def exclusive(self, timeout: float = 30.0):
        """Cross-process advisory lock for multi-statement write phases.

        busy_timeout keeps individual transactions safe under concurrent
        sessions; this serializes whole reindex passes (cold build, sync) so
        two tether processes on the same repo can't interleave one. flock is
        advisory and per-file, released automatically if the process dies.
        Raises TimeoutError when another session holds the lock past timeout.
        """
        lock_path = self.path.with_suffix(".lock")
        fh = open(lock_path, "w")
        deadline = time.monotonic() + timeout
        try:
            while True:
                try:
                    fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(
                            f"another tether session holds the model lock ({lock_path})"
                        )
                    time.sleep(0.1)
            yield
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(fh, fcntl.LOCK_UN)
            fh.close()

    def _commit(self) -> None:
        self._conn.commit()

    # -- meta --------------------------------------------------------------
    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
            self._commit()

    def get_meta(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    # -- files -------------------------------------------------------------
    def set_file(self, path: str, content_hash: str, commit_hash: str, indexed_at: float) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO files(path, content_hash, commit_hash, indexed_at) "
                "VALUES(?,?,?,?) ON CONFLICT(path) DO UPDATE SET "
                "content_hash=excluded.content_hash, commit_hash=excluded.commit_hash, "
                "indexed_at=excluded.indexed_at",
                (path, content_hash, commit_hash, indexed_at),
            )
            self._commit()

    def get_file_hash(self, path: str) -> str | None:
        row = self._conn.execute("SELECT content_hash FROM files WHERE path=?", (path,)).fetchone()
        return row["content_hash"] if row else None

    def all_files(self) -> list[str]:
        return [r["path"] for r in self._conn.execute("SELECT path FROM files")]

    def delete_file(self, path: str) -> None:
        """Remove a file and everything the substrate derived from it (nodes +
        edges originating in it). Beliefs are NOT touched here — they're handled
        by invalidation, since a deleted file may still be cited historically."""
        with self._lock:
            self._conn.execute("DELETE FROM edges WHERE path=?", (path,))
            self._conn.execute("DELETE FROM nodes WHERE path=?", (path,))
            self._conn.execute("DELETE FROM files WHERE path=?", (path,))
            self._commit()

    # -- nodes -------------------------------------------------------------
    def upsert_nodes(self, nodes: list[Node]) -> None:
        if not nodes:
            return
        with self._lock:
            self._conn.executemany(
                "INSERT INTO nodes(id, kind, name, path, lineno, end_lineno, module, content_hash, slice_hash) "
                "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                "kind=excluded.kind, name=excluded.name, path=excluded.path, "
                "lineno=excluded.lineno, end_lineno=excluded.end_lineno, "
                "module=excluded.module, content_hash=excluded.content_hash, "
                "slice_hash=excluded.slice_hash",
                [(n.id, n.kind, n.name, n.path, n.lineno, n.end_lineno, n.module,
                  n.content_hash, n.slice_hash)
                 for n in nodes],
            )
            self._commit()

    def delete_nodes_in_file(self, path: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM nodes WHERE path=?", (path,))
            self._commit()

    def get_node(self, node_id: str) -> Node | None:
        row = self._conn.execute("SELECT * FROM nodes WHERE id=?", (node_id,)).fetchone()
        return self._row_to_node(row) if row else None

    def nodes_in_file(self, path: str) -> list[Node]:
        rows = self._conn.execute("SELECT * FROM nodes WHERE path=? ORDER BY lineno", (path,))
        return [self._row_to_node(r) for r in rows]

    def nodes_in_module(self, module: str) -> list[Node]:
        rows = self._conn.execute("SELECT * FROM nodes WHERE module=?", (module,))
        return [self._row_to_node(r) for r in rows]

    def find_nodes_by_name(self, name: str) -> list[Node]:
        """Exact-qualname match plus a trailing-segment match (so ``refund`` finds
        ``RefundService.refund``)."""
        rows = self._conn.execute(
            "SELECT * FROM nodes WHERE name=? OR name LIKE ?",
            (name, f"%.{name}"),
        )
        return [self._row_to_node(r) for r in rows]

    def all_nodes(self) -> list[Node]:
        return [self._row_to_node(r) for r in self._conn.execute("SELECT * FROM nodes")]

    def count_nodes(self) -> int:
        return self._conn.execute("SELECT COUNT(*) AS c FROM nodes").fetchone()["c"]

    def all_modules(self) -> list[str]:
        rows = self._conn.execute(
            "SELECT DISTINCT module FROM nodes WHERE module != '' ORDER BY module")
        return [r["module"] for r in rows]

    def node_slice_hashes(self, path: str) -> dict[str, str]:
        """{qualname: slice_hash} for every node in a file — the before/after
        snapshot symbol-granular invalidation diffs across a reindex."""
        rows = self._conn.execute(
            "SELECT name, slice_hash FROM nodes WHERE path=?", (path,))
        return {r["name"]: r["slice_hash"] or "" for r in rows}

    @staticmethod
    def _row_to_node(r: sqlite3.Row) -> Node:
        keys = r.keys()
        return Node(id=r["id"], kind=r["kind"], name=r["name"], path=r["path"],
                    lineno=r["lineno"], end_lineno=r["end_lineno"],
                    module=r["module"], content_hash=r["content_hash"],
                    slice_hash=(r["slice_hash"] or "") if "slice_hash" in keys else "")

    # -- edges -------------------------------------------------------------
    def insert_edges(self, edges: list[Edge]) -> None:
        if not edges:
            return
        with self._lock:
            self._conn.executemany(
                "INSERT OR IGNORE INTO edges(src, dst, kind, path, lineno, resolved) "
                "VALUES(?,?,?,?,?,?)",
                [(e.src, e.dst, e.kind, e.path, e.lineno, int(e.resolved)) for e in edges],
            )
            self._commit()

    def delete_edges_in_file(self, path: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM edges WHERE path=?", (path,))
            self._commit()

    def edges_from(self, src: str, kind: str | None = None) -> list[Edge]:
        if kind:
            rows = self._conn.execute("SELECT * FROM edges WHERE src=? AND kind=?", (src, kind))
        else:
            rows = self._conn.execute("SELECT * FROM edges WHERE src=?", (src,))
        return [self._row_to_edge(r) for r in rows]

    def edges_to(self, dst: str, kind: str | None = None) -> list[Edge]:
        if kind:
            rows = self._conn.execute("SELECT * FROM edges WHERE dst=? AND kind=?", (dst, kind))
        else:
            rows = self._conn.execute("SELECT * FROM edges WHERE dst=?", (dst,))
        return [self._row_to_edge(r) for r in rows]

    def edges_by_kind(self, kind: str) -> list[Edge]:
        rows = self._conn.execute("SELECT * FROM edges WHERE kind=?", (kind,))
        return [self._row_to_edge(r) for r in rows]

    def all_edges(self) -> list[Edge]:
        return [self._row_to_edge(r) for r in self._conn.execute("SELECT * FROM edges")]

    @staticmethod
    def _row_to_edge(r: sqlite3.Row) -> Edge:
        return Edge(src=r["src"], dst=r["dst"], kind=r["kind"], path=r["path"],
                    lineno=r["lineno"], resolved=bool(r["resolved"]))

    # -- beliefs -----------------------------------------------------------
    def put_belief(self, b: Belief) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO beliefs(id, claim, confidence, justified_by, kind, on_conflict, "
                "verified, confirmations, last_consulted, stale, created, source) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                "claim=excluded.claim, confidence=excluded.confidence, "
                "justified_by=excluded.justified_by, kind=excluded.kind, "
                "on_conflict=excluded.on_conflict, verified=excluded.verified, "
                "confirmations=excluded.confirmations, last_consulted=excluded.last_consulted, "
                "stale=excluded.stale, created=excluded.created, source=excluded.source",
                (b.id, b.claim, float(b.confidence), json.dumps(b.justified_by), b.kind,
                 b.on_conflict, b.verified, b.confirmations, b.last_consulted,
                 int(b.stale), b.created, b.source),
            )
            # rewrite evidence index for this belief
            self._conn.execute("DELETE FROM evidence WHERE belief_id=?", (b.id,))
            for raw in b.justified_by:
                file, symbol, commit = _parse_citation_light(raw)
                self._conn.execute(
                    "INSERT INTO evidence(belief_id, file, symbol, commit_hash, raw) "
                    "VALUES(?,?,?,?,?)", (b.id, file, symbol, commit, raw))
            self._commit()

    def get_belief(self, belief_id: str) -> Belief | None:
        row = self._conn.execute("SELECT * FROM beliefs WHERE id=?", (belief_id,)).fetchone()
        return self._row_to_belief(row) if row else None

    def all_beliefs(self) -> list[Belief]:
        return [self._row_to_belief(r) for r in self._conn.execute("SELECT * FROM beliefs")]

    def count_beliefs(self) -> int:
        return self._conn.execute("SELECT COUNT(*) AS c FROM beliefs").fetchone()["c"]

    def delete_belief(self, belief_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM beliefs WHERE id=?", (belief_id,))
            self._conn.execute("DELETE FROM evidence WHERE belief_id=?", (belief_id,))
            self._commit()

    def beliefs_citing_file(self, path: str) -> list[str]:
        """Belief ids whose evidence cites ``path`` — the invalidation query. O(log n)
        via the evidence file index."""
        rows = self._conn.execute(
            "SELECT DISTINCT belief_id FROM evidence WHERE file=?", (path,))
        return [r["belief_id"] for r in rows]

    def beliefs_citing(self, path: str) -> list[tuple[str, str]]:
        """(belief_id, symbol) pairs citing ``path`` — symbol may be "" for a
        whole-file citation. Lets invalidation stale-mark only the beliefs whose
        cited *symbol* actually changed."""
        rows = self._conn.execute(
            "SELECT DISTINCT belief_id, symbol FROM evidence WHERE file=?", (path,))
        return [(r["belief_id"], r["symbol"] or "") for r in rows]

    def mark_beliefs_stale(self, belief_ids: list[str]) -> None:
        if not belief_ids:
            return
        with self._lock:
            self._conn.executemany(
                "UPDATE beliefs SET stale=1 WHERE id=?", [(bid,) for bid in belief_ids])
            self._commit()

    def touch_belief(self, belief_id: str, ts: float) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE beliefs SET last_consulted=? WHERE id=?", (ts, belief_id))
            self._commit()

    def beliefs_by_lru(self, limit: int) -> list[Belief]:
        """The ``limit`` least-recently-consulted beliefs (eviction candidates)."""
        rows = self._conn.execute(
            "SELECT * FROM beliefs ORDER BY last_consulted ASC, confidence ASC LIMIT ?",
            (limit,))
        return [self._row_to_belief(r) for r in rows]

    @staticmethod
    def _row_to_belief(r: sqlite3.Row) -> Belief:
        return Belief(
            id=r["id"], claim=r["claim"], confidence=r["confidence"],
            justified_by=json.loads(r["justified_by"] or "[]"), kind=r["kind"],
            on_conflict=r["on_conflict"], verified=r["verified"] or "",
            confirmations=r["confirmations"] or 1, last_consulted=r["last_consulted"] or 0.0,
            stale=bool(r["stale"]), created=r["created"] or 0.0, source=r["source"] or "inferred")

    # -- invariants --------------------------------------------------------
    def put_invariant(self, inv: Invariant) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO invariants(id, claim, check_sexpr, enforcement, confidence, "
                "on_conflict, counterexample, source, created) VALUES(?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET claim=excluded.claim, "
                "check_sexpr=excluded.check_sexpr, enforcement=excluded.enforcement, "
                "confidence=excluded.confidence, on_conflict=excluded.on_conflict, "
                "counterexample=excluded.counterexample, source=excluded.source",
                (inv.id, inv.claim, inv.check, inv.enforcement, str(inv.confidence),
                 inv.on_conflict, inv.counterexample, inv.source, inv.created),
            )
            self._commit()

    def get_invariant(self, inv_id: str) -> Invariant | None:
        row = self._conn.execute("SELECT * FROM invariants WHERE id=?", (inv_id,)).fetchone()
        return self._row_to_invariant(row) if row else None

    def all_invariants(self) -> list[Invariant]:
        return [self._row_to_invariant(r) for r in self._conn.execute("SELECT * FROM invariants")]

    def delete_invariant(self, inv_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM invariants WHERE id=?", (inv_id,))
            self._commit()

    @staticmethod
    def _row_to_invariant(r: sqlite3.Row) -> Invariant:
        conf: object = r["confidence"]
        if conf != "confirmed":
            try:
                conf = float(conf)
            except (TypeError, ValueError):
                conf = 0.5
        return Invariant(
            id=r["id"], claim=r["claim"], check=r["check_sexpr"] or "",
            enforcement=r["enforcement"], confidence=conf, on_conflict=r["on_conflict"],
            counterexample=r["counterexample"] or "", source=r["source"] or "inferred",
            created=r["created"] or 0.0)

    # -- decisions ---------------------------------------------------------
    def put_decision(self, d: Decision) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO decisions(id, status, detector, accepted_pattern, reason, "
                "created, archived) VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                "status=excluded.status, detector=excluded.detector, "
                "accepted_pattern=excluded.accepted_pattern, reason=excluded.reason, "
                "archived=excluded.archived",
                (d.id, d.status, d.detector, d.accepted_pattern, d.reason,
                 d.created, int(d.archived)),
            )
            self._commit()

    def get_decision(self, dec_id: str) -> Decision | None:
        row = self._conn.execute("SELECT * FROM decisions WHERE id=?", (dec_id,)).fetchone()
        return self._row_to_decision(row) if row else None

    def all_decisions(self, include_archived: bool = False) -> list[Decision]:
        if include_archived:
            rows = self._conn.execute("SELECT * FROM decisions")
        else:
            rows = self._conn.execute("SELECT * FROM decisions WHERE archived=0")
        return [self._row_to_decision(r) for r in rows]

    @staticmethod
    def _row_to_decision(r: sqlite3.Row) -> Decision:
        return Decision(
            id=r["id"], status=r["status"], detector=r["detector"] or "",
            accepted_pattern=r["accepted_pattern"] or "", reason=r["reason"] or "",
            created=r["created"] or 0.0, archived=bool(r["archived"]))

    # -- invariant results -------------------------------------------------
    def record_invariant_result(self, inv_id: str, passed: bool, counterexample: str,
                                run_at: float, commit_hash: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO invariant_results(invariant_id, passed, counterexample, run_at, "
                "commit_hash) VALUES(?,?,?,?,?) ON CONFLICT(invariant_id, commit_hash) DO UPDATE "
                "SET passed=excluded.passed, counterexample=excluded.counterexample, "
                "run_at=excluded.run_at",
                (inv_id, int(passed), counterexample, run_at, commit_hash),
            )
            self._commit()

    def get_invariant_result(self, inv_id: str, commit_hash: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM invariant_results WHERE invariant_id=? AND commit_hash=?",
            (inv_id, commit_hash)).fetchone()
        if not row:
            return None
        return {"invariant_id": row["invariant_id"], "passed": bool(row["passed"]),
                "counterexample": row["counterexample"], "run_at": row["run_at"],
                "commit_hash": row["commit_hash"]}
