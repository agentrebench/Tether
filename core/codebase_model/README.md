# `core/codebase_model` — Persistent Codebase Mental Model (implementation)

The design spec lives in [`docs/codebase-mental-model/`](../../docs/codebase-mental-model/).
This is the working implementation of it. The interface every module conforms to
is frozen in [`CONTRACT.md`](CONTRACT.md).

> **What it is.** A persistent, queryable, incrementally-updated model of a repo
> the agent consults *before* grep/embeddings — so it reasons over accumulated
> understanding instead of rebuilding it every session. Two layers, opposite cost
> models: a regenerable **derived substrate** (parser ground truth) and a lazy,
> defeasible **inferred layer** (beliefs / invariants / decisions).

## Module map

| File | Layer | Responsibility |
|---|---|---|
| `model.py` | — | Dataclasses + vocabulary: `Node`/`Edge`, `Belief`/`Invariant`/`Decision`/`Violation`, and the `OnConflict`/`Enforcement`/`BeliefKind` policy constants. |
| `sexpr.py` | — | Real s-expression reader/printer (the doc-08 representation idiom; compiled checks are stored as s-exprs). |
| `store.py` | truth | SQLite single source of truth (WAL, B-tree indexes). Nodes/edges/beliefs/evidence/invariants/decisions/results as rows. |
| `substrate.py` | derived | Python-AST extractor → nodes + `calls`/`imports`/`contains`/`inherits`/`writes` edges. `O(Δ)` per file. |
| `citations.py` | — | Content-addressed evidence pointers (`file @ symbol @ commit`) + slice re-fetch. |
| `indexer.py` | derived | Cold build (`O(n)`) + incremental update (`O(Δ)`), git integration, reverse-reachability blast radius. |
| `beliefs.py` | inferred | Descriptive belief cache: supersession, `:demote` conflict policy, LRU cap, lazy re-verify == drift == eviction. |
| `invariants.py` | inferred | The invariant compiler: `:check` s-exprs → deterministic graph queries returning counterexamples; asymmetric enforcement. |
| `query.py` | read | The agent-facing surface: `affects` / `owns` / `allowed` / `architecture_index` / `answer`. |
| `service.py` | facade | `CodebaseModel` wiring + per-repo singleton (`get_model`), build/sync/`on_edit`/`teardown_substrate`. |
| `acceptance.py` | test | The runnable benchmark (doc 11): seed → **delete substrate** → answer from beliefs + re-fetch citations → score. |

Agent tools are in [`tools/codebase_model_tool.py`](../../tools/codebase_model_tool.py):
`model_query` (read), `model_record` (write), `model_check` (run rules vs. a diff).

## The three belief types (the crux)

Conflict resolution **branches on type** — one uniform update rule would launder
every architectural violation into "model is stale":

- **derived fact** → `:refresh` (re-parse; code is the source)
- **descriptive belief** → `:demote` (code wins; `beliefs.demote`, deletes below floor)
- **prescriptive invariant** → `:raise` a violation (judges the code; never self-weakens)

Enforcement is **asymmetric**: a `Violation` is only `.blocking` when its invariant
`may_hard_block()` (compiled-and-checked *or* human-confirmed). Soft/inferred
invariants warn, never veto — a false invariant is worse than a missing one.

## Using it

Opt in via config (`~/.tether/config.json`):

```json
{ "codebase_model_enabled": true }
```

Then in the REPL:

```
/persistence build          # cold-index this repo (O(n), once)
/persistence                # status: symbols, edges, beliefs, invariants
/persistence sync           # incremental refresh (also runs automatically on startup)
/persistence check [files]  # run invariants + rejected-pattern detectors vs. a diff
/persistence ask <question> # affects / owns / allowed / architecture, routed
```

(`/model` in the REPL is the LLM you're running — switch model, token usage —
not this feature; the codebase model lives at `/persistence`.)

Once built, the substrate stays fresh automatically: every successful
`file_edit`/`file_write` fires an edit-hook (`CodebaseModel.on_edit`) that
re-indexes just that file and invalidates beliefs citing it. The agent gets a
system-prompt nudge to consult the model before retrieval, and the
`model_query`/`model_check`/`model_record` tools.

Programmatic:

```python
from tether.core.codebase_model.service import get_model
m = get_model()          # per-repo singleton, db under ~/.tether/models/<hash>.db
m.build()
m.affects("RefundService.refund")          # blast radius
m.owns("refunds")                          # ownership recall
m.allowed("construct a singleton cache")   # rule check
m.record_belief("(owns billing refunds)", justified_by=["billing/refunds.py @ RefundService.refund"])
```

Run the acceptance benchmark (the "did it learn?" bar):

```python
import tempfile
from tether.core.codebase_model.acceptance import build_fixture, AcceptanceHarness
m = build_fixture(tempfile.mkdtemp())
res = AcceptanceHarness(m).run([
    {"kind": "owns", "topic": "refunds", "expect_contains": ["billing"]},
    {"kind": "allowed", "description": "a plugin constructs the Renderer",
     "expect_contains": ["plugins"], "expect_blocked": True},
])
print(res.passed, res.score)   # True {'coverage': 1.0, 'refetch_precision': 1.0, ...}
```

## Deliberate deviations from the spec

The spec targets a language-agnostic system with a Lisp query surface; this first
implementation is scoped to fit the Tether (Python) harness. Documented so the
gap is intentional, not accidental:

1. **SQLite, not a bespoke store.** `sqlite3` is stdlib, so it honors Tether's
   "no external deps" rule while delivering the spec's B-tree-indexed
   `O(log n + k)` queries, WAL, and single-writer model. (Tether is otherwise
   JSON-on-disk; this is the one place SQLite earns its keep.)
2. **Python query surface, real s-exprs.** Reasoning runs in Python (`query.py`,
   `invariants.py`) rather than an embedded Lisp image — but `sexpr.py` is a
   genuine reader/printer, compiled invariant checks *are* s-exprs, and the
   agent-facing projection is s-expressions, so the doc-08 idiom is real.
3. **Substrate is Python-only** (`ast`). The architecture is language-agnostic;
   only the extractor is Python-specific. Adding a language = adding an extractor
   that emits the same `Node`/`Edge` rows.
4. **Name binding is best-effort.** `calls`/`inherits` edges store the callee's
   simple name; reverse reachability matches on name. Good enough for blast radius
   and the compiled checks, as the spec allows; a fuller resolution pass is future
   work.
5. **Not yet built:** the architecture-graph recovery (HTTP/queue/SQL/DI edges),
   the dominator-based "writes-through-UoW" check (only `forbidden-edge` and
   `no-global-construction` compile today), and mining invariants from
   PR comments/commit messages. The `WRITES` sink edges and the s-expr evaluator
   are in place as the substrate for those.

## Tests

`tests/test_cbm_*.py` (one per module, `unittest`). Run:

```
python3 -m pytest tests/test_cbm_*.py -q
```
