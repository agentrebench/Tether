# codebase_model — implementation contract

This file freezes the public interface of every module so they can be built
independently and still compose. **Do not deviate from these signatures.** The
foundation modules below are already implemented and verified — call them exactly
as shown. Stdlib-only. Python ≥3.10. `from __future__ import annotations` at the
top of every file.

## Frozen foundation (already implemented — import and use)

### `model.py`
- `Node(id, kind, name, path, lineno=0, end_lineno=0, module="", content_hash="")`
- `Edge(src, dst, kind, path="", lineno=0, resolved=False)`
- `Belief(id, claim, confidence=0.5, justified_by=[], kind=BeliefKind.DESCRIPTIVE, on_conflict=OnConflict.DEMOTE, verified="", confirmations=1, last_consulted=0.0, stale=False, created=0.0, source="inferred")` — has `.touch(ts)`
- `Invariant(id, claim, check="", enforcement=Enforcement.SOFT, confidence=0.5, on_conflict=OnConflict.RAISE, counterexample="", source="inferred", created=0.0)` — props `.compiled`, `.confirmed`, `.may_hard_block()`
- `Decision(id, status="rejected", detector="", accepted_pattern="", reason="", created=0.0, archived=False)`
- `Violation(invariant_id, claim, location, detail="", enforcement=Enforcement.SOFT)` — prop `.blocking`
- Constants classes: `NodeKind` (MODULE/CLASS/FUNCTION/METHOD/VARIABLE), `EdgeKind` (CALLS/IMPORTS/CONTAINS/INHERITS/REFERENCES/WRITES), `BeliefKind` (DERIVED/DESCRIPTIVE/PRESCRIPTIVE), `OnConflict` (REFRESH/DEMOTE/RAISE), `Enforcement` (HARD/SOFT)
- `node_id(path, qualname="") -> str` (`"path::qualname"`); `module_of(path) -> str`
- `DEFAULT_ON_CONFLICT: dict[beliefkind -> onconflict]`

### `store.py` — `ModelStore(db_path)`
nodes: `upsert_nodes(list[Node])`, `delete_nodes_in_file(path)`, `get_node(id)`, `nodes_in_file(path)`, `nodes_in_module(module)`, `find_nodes_by_name(name)` (matches exact qualname OR trailing `.name`), `all_nodes()`, `count_nodes()`, `all_modules()`
edges: `insert_edges(list[Edge])`, `delete_edges_in_file(path)`, `edges_from(src, kind=None)`, `edges_to(dst, kind=None)`, `edges_by_kind(kind)`, `all_edges()`
files: `set_file(path, content_hash, commit_hash, indexed_at)`, `get_file_hash(path)`, `all_files()`, `delete_file(path)` (drops nodes+edges+file row)
beliefs: `put_belief(Belief)` (also rewrites evidence index), `get_belief(id)`, `all_beliefs()`, `count_beliefs()`, `delete_belief(id)`, `beliefs_citing_file(path) -> list[id]`, `mark_beliefs_stale(list[id])`, `touch_belief(id, ts)`, `beliefs_by_lru(limit) -> list[Belief]`
invariants: `put_invariant(Invariant)`, `get_invariant(id)`, `all_invariants()`, `delete_invariant(id)`
decisions: `put_decision(Decision)`, `get_decision(id)`, `all_decisions(include_archived=False)`
results: `record_invariant_result(inv_id, passed, counterexample, run_at, commit_hash)`, `get_invariant_result(inv_id, commit_hash)`
meta: `set_meta(key, value)`, `get_meta(key)`; lifecycle: `close()`

### `substrate.py`
- `extract(path, content) -> (list[Node], list[Edge])` — `path` is repo-relative posix
- `extract_symbol_source(content, qualname) -> (source, lineno, end_lineno) | None`
- `file_content_hash(text) -> str`
- CALLS/INHERITS/WRITES edges store the callee **simple name** in `dst` with `resolved=False`; CONTAINS edges are resolved (dst is a node id). WRITES dst is `"sink:<method>"`. Module-scope calls have `src` = the module node id (kind MODULE).

### `citations.py`
- `Citation(file, symbol="", commit="")` — `.format()`
- `parse(raw) -> Citation`; `format_citation(file, symbol="", commit="") -> str`
- `refetch(citation, repo_root) -> RefetchResult(citation, found, source="", lineno=0, end_lineno=0, error="")`

---

## Modules to implement (build these)

Each module gets ONE source file + ONE test file `tests/test_cbm_<module>.py`
(unittest.TestCase, `sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))`,
imports `from tether.core.codebase_model.<module> import ...`, stdlib-only,
use `tempfile.mkdtemp()` for the db).

### `indexer.py` — `Indexer(store: ModelStore, repo_root: Path)`
- `current_commit() -> str` — `git -C root rev-parse --short HEAD`; `""` if no git/error.
- `discover_python_files() -> list[str]` — repo-relative posix `*.py`, skipping `.git`, `__pycache__`, `*.egg-info`, `venv`/`.venv`/`node_modules`, and the model db.
- `index_file(rel_path, content=None, commit="") -> bool` — read file if content None; skip (return False) if `store.get_file_hash`==current hash; else delete old nodes/edges in file, `substrate.extract`, upsert, `set_file`. Return True if indexed.
- `remove_file(rel_path)` — `store.delete_file`.
- `cold_build(paths=None) -> dict` — index all discovered (or given) files. Return `{"indexed": n, "files": total}`. O(n).
- `update(changed, removed=None, commit="") -> dict` — reindex `changed`, remove `removed`. Return `{"indexed": n, "removed": m}`. O(Δ).
- `changed_since_index() -> (changed, removed)` — compare disk content-hash vs `store.get_file_hash` over discovered files + files in store; a file in store but missing on disk is removed.
- `blast_radius(target, max_depth=6) -> list[str]` — reverse dependency closure as **node ids**. Resolve `target` (a node id or a name via `find_nodes_by_name`) to node(s); BFS over callers using `store.edges_to(node.name, kind=CALLS)` whose `src` are caller node ids; for each caller recurse on its name. Dedupe, exclude the target itself, cap depth. Best-effort name binding (document it).
- `affected_files(rel_path, max_depth=6) -> list[str]` — union of paths of nodes in the blast radius of every symbol defined in `rel_path`.

### `beliefs.py` — `BeliefManager(store, *, max_beliefs=500, verifier=None, clock=time.time)`
`verifier: Callable[[Belief, list[RefetchResult]], bool] | None` (None ⇒ trust on refetch-found).
- `add(claim, *, confidence=0.7, justified_by=None, source="inferred", belief_id=None, kind=BeliefKind.DESCRIPTIVE) -> Belief` — `belief_id` defaults to `slugify(claim)`. **Supersession:** if id exists, REPLACE inheriting provenance (`confirmations = old+1`, keep older `created`), raise confidence toward max(old, new). Set `created`/`verified` via clock. Enforce cap with `_evict()` after add.
- `get(id) -> Belief | None`
- `consult(id) -> Belief | None` — touch (LRU via `store.touch_belief`); return the belief. (Re-verification is triggered by the service when stale; keep consult cheap.)
- `reinforce(id) -> Belief | None` — confidence toward 1 (e.g. `+= (1-c)*0.5`), `confirmations += 1`, clear stale, update verified.
- `demote(id, amount=0.25) -> Belief | None` — `confidence -= amount`; if `< 0.15` delete and return None (drift+eviction unified). This is the descriptive `:demote` conflict policy.
- `invalidate(changed_files) -> list[str]` — `store.beliefs_citing_file` for each, `store.mark_beliefs_stale`, return ids.
- `reverify(id, repo_root) -> bool` — refetch each citation (`citations.parse`+`refetch`); if any not found ⇒ demote and return False; else run `verifier` (or trust) ⇒ if True `reinforce` (clear stale, set verified) return True, else `demote` return False. **This is lazy re-verification: only call when a stale belief is actually consulted.**
- `all() -> list[Belief]`; `_evict() -> int` — while `count_beliefs() > max_beliefs`, drop lowest `beliefs_by_lru`.
- module fn `slugify(text) -> str` (kebab, like core/skills.py).

### `invariants.py` — `InvariantEngine(store)`
Compiled-check s-expr vocabulary (parse with `sexpr.read`; everything else ⇒ uncompilable ⇒ soft):
- `(forbidden-edge :kind <edgekind> :from <pat> :to <pat>)` — violation per matching edge; `:from` matches the **src node's module or name**, `:to` matches the **dst** (name). `<pat>` uses `fnmatch` (`*` glob), and bare string also matches as a path/module prefix.
- `(no-global-construction :class <Name>)` — violation per CALLS edge whose `src` node kind is MODULE and `dst` == `<Name>`.
Decision detector s-expr vocabulary (for `detect_rejected`):
- `(construct <Name>)` — matches a module-scope CALLS edge to `<Name>` in a changed file.
- `(call-name <name> :from <pat>)` — matches a CALLS edge to `<name>` from a src matching `<pat>`.
Methods:
- `add(claim, *, check="", enforcement=Enforcement.SOFT, confidence=0.5, source="inferred", inv_id=None) -> Invariant` (`inv_id` default slug of claim; persist).
- `add_decision(*, reason, detector="", accepted_pattern="", status="rejected", dec_id=None) -> Decision`.
- `compile_check(check_sexpr) -> Callable[[list[Edge]], list[Violation]] | None` — returns evaluator over a list of edges, or None if head unknown/parse fails.
- `run(inv, *, edges=None) -> list[Violation]` — compile+eval over `edges` (default `store.all_edges()` of relevant kind). Resolve each violation's `location` to `src_node.path:edge.lineno` (look up `store.get_node(edge.src)`). Carry `inv.enforcement`.
- `check_all(commit="") -> list[Violation]` — run every compiled invariant; `store.record_invariant_result` per invariant (passed = no violations); set `inv.counterexample` + persist. Soft+uncompiled invariants are skipped (return nothing).
- `check_diff(changed_files, commit="") -> list[Violation]` — like check_all but only over edges whose `path in changed_files`.
- `detect_rejected(changed_files) -> list[Violation]` — run each non-archived decision's detector over edges in changed_files; Violation uses `invariant_id=decision.id`, `claim=decision.reason`, `enforcement=SOFT`.
- Enforcement: a `Violation` is `.blocking` only when its invariant `may_hard_block()`. `run` must set `enforcement=Enforcement.HARD` on the violation only if `inv.may_hard_block()`, else SOFT.

### `query.py` — `QuerySurface(store, indexer, beliefs, invariants)`
- `affects(target) -> dict` → `{"target", "affected_symbols": [node_id...], "affected_files": [...], "count"}` (uses `indexer.blast_radius` / `affected_files`).
- `owns(topic) -> dict` → consult descriptive beliefs whose claim contains `owns` and the topic token; `{"topic", "beliefs": [{"id","claim","confidence","citations","stale"}], "answer": str}`. Call `beliefs.consult` on each hit (LRU touch).
- `allowed(description, changed_files=None) -> dict` → run `invariants.check_diff`(or check_all) + `detect_rejected`; match `description` tokens against invariant claims/decision reasons to surface the relevant ones; `{"allowed": bool, "violations": [...], "blocking": bool}`. `allowed` is False if any blocking violation.
- `architecture_index() -> str` — the small "load first" summary: module list w/ node counts, belief count, and compiled/confirmed invariants as s-exprs. Keep ~<2KB.
- `project_facts(node_id) -> str` — s-exprs `(fact (calls A B) :at <commit>)` for the node's out-edges, via `sexpr.write`.
- `answer(question) -> str` — keyword route: "affect|impact|change" ⇒ affects; "own|responsib" ⇒ owns; "allow|can i|pattern|should" ⇒ allowed; else architecture_index. Return human-readable text.

### `service.py` — `CodebaseModel`
- `__init__(self, repo_root, db_path=None, *, max_beliefs=500, verifier=None)` — wires `ModelStore`, `Indexer`, `BeliefManager`, `InvariantEngine`, `QuerySurface`. `db_path` default = `model_db_path(repo_root)`.
- module fn `model_db_path(repo_root) -> Path` = `CONFIG_DIR/"models"/f"{sha1(abspath)[:16]}.db"` (import `from ..config import CONFIG_DIR`).
- module-level cache `get_model(cwd=None) -> CodebaseModel` (one instance per resolved repo root; like skills `default_store`).
- `build() -> dict` (cold_build + set meta commit). `sync() -> dict` (changed_since_index → update → invalidate beliefs for changed → set meta commit).
- `on_edit(rel_path)` — `indexer.index_file` + `beliefs.invalidate([rel_path])`. (Edit-hook entry point; must never raise — wrap.)
- delegate: `affects/owns/allowed/architecture_index/answer` → query; `record_belief(...)`/`record_invariant(...)`/`record_decision(...)` → managers.
- `teardown_substrate()` — delete all nodes/edges/files rows (keep beliefs/invariants/decisions). Implement by iterating `store.all_files()` → `store.delete_file`. For the acceptance test.
- expose `.store .indexer .beliefs .invariants .query .repo_root`.

### `acceptance.py`
- `@dataclass AcceptanceResult(passed: bool, probes: list[dict], score: dict)`
- `AcceptanceHarness(model: CodebaseModel)` with `run(probes) -> AcceptanceResult`. Each probe: `{"kind": "owns"|"allowed"|"affects", ...query args..., "expect_contains": [substrings], "expect_blocked": bool?}`. Procedure: (1) before teardown, for "affects" probes, capture the live answer into a descriptive belief with a citation (simulating the agent *learning* it); (2) `model.teardown_substrate()`; (3) answer each probe via beliefs/invariants ONLY and, where a belief has citations, `citations.refetch` to verify the slice still resolves; score each probe on answered-from-model AND (for cited beliefs) refetch-found. Aggregate `passed`.
- `build_fixture(root: Path) -> CodebaseModel` — write a tiny multi-file fixture repo under `root` (a `billing/` with refunds+uow, a `plugins/` that must not call Renderer, a `render/` with Renderer), build the model, seed: a descriptive ownership belief (billing owns refunds, cited), a confirmed compiled forbidden-edge invariant (plugins → Renderer, HARD), a rejected-singleton decision. Return the model. Used by tests and as a runnable demo.

### `tools/codebase_model_tool.py` (in the `tools/` package, not codebase_model)
`from ..core.codebase_model.service import get_model` ; `from ..core.models import ToolResult` ; `from .base import BaseTool`.
- `ModelQueryTool` name `model_query`: params `{action: enum[affects,owns,allowed,architecture], target?: str}`. Read-only. `execute` → `get_model().answer(...)` or specific method; return ToolResult.
- `ModelRecordTool` name `model_record`: params `{kind: enum[belief,invariant,decision], claim?, reason?, confidence?, citations?: [str], check?, detector?, accepted_pattern?}`. Write. Returns confirmation.
- `ModelCheckTool` name `model_check`: params `{changed_files?: [str]}` → run invariants/decisions against the diff (or all), return violations text (mark blocking ones).
All tools must degrade gracefully (helpful message, `is_error=False`) when the model is empty/unbuilt.

## Conventions
- Never let a hook/tool raise into the engine — catch and return a message.
- `time.time` injected as `clock` where tests need determinism.
- Keep each module importable on its own (import only foundation + its declared deps).
