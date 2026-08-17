# Glossary

Every load-bearing term in this directory, in one place. Where a term has a home
file, it's linked.

### Derived substrate
The parser-produced, regenerable [layer](01-architecture-split.md): AST → symbols
→ name binding/types → call graph → dependency graph, plus the dynamic pillar
(CFG, data-flow, aggregated trace/coverage) and data pillar (schema, types, I/O
surface). A materialized view of HEAD. No confidence scores. Cost: `O(Δ)`.

### Inferred layer
The LLM-produced [layer](01-architecture-split.md): modules, ownership,
descriptive beliefs, decisions, invariants. Not regenerable for free (re-inference
= an LLM call). A cache, built lazily. Where hallucination lives.

### Architecture graph
The module-to-module wiring, **recovered + interpreted** rather than derived —
needs edges the substrate can't see (HTTP, queues, SQL strings, DI). Sits between
the two layers; gets its own treatment. See [01](01-architecture-split.md).

### Derived fact
A ground-truth relation from the substrate, e.g. `(calls A B)`. No confidence.
On conflict with code: **`:refresh`** (re-parse). See [02](02-belief-types.md).

### Descriptive belief
A defeasible LLM claim *about* the system, e.g. `(owns billing refunds)`. Carries
confidence and content-addressed citations. On conflict with code: **`:demote`**
(code wins). See [02](02-belief-types.md).

### Prescriptive belief
An invariant or rejected pattern — a rule the code is *supposed to obey*. On
conflict with code: **`:raise` a violation** (NOT `:mark-stale`). The inversion
that is the point of the system. See [02](02-belief-types.md).

### `:on-conflict`
The first-class policy field on every belief: `:refresh` (facts) | `:demote`
(descriptive) | `:raise` (prescriptive). Storing it per-belief forces the
three-way branch. See [02](02-belief-types.md).

### Invariant compiler
The mechanism that turns an invariant into a **deterministic graph query** that
runs on every diff and returns a counterexample with a location — instead of a
soft LLM-eyeballed assertion. The highest-leverage piece. See
[03](03-invariant-compiler.md).

### Counterexample
The output of a compiled invariant check when it fails: a pass/failed result with
a location, e.g. "`RefundService.retry` writes at line 88 outside any UnitOfWork
frame." A finding, not a belief.

### Asymmetric enforcement
The rule that a *false* invariant (vetoes a correct edit) is worse than a
*missing* one (misses a bug). So: hard-block only on compiled/confirmed
invariants; soft-warn on inferred ones. See [03](03-invariant-compiler.md).

### Content-addressed citation / evidence pointer
A belief's justification addressed *into the repo*: `file @ symbol @ commit`, e.g.
`billing/refunds.py @ RefundService.refund @ abc123`. Survives deleting the
substrate; drives re-verification, invalidation, and commit-keyed caching. See
[04](04-learned-not-decorative.md).

### Learned (vs. decorative)
A belief is **learned** iff: (1) stated in its own terms, (2) re-derivable from
its content-addressed citation, (3) load-bearing (actually consulted and changes
behavior). Otherwise decorative. See [00](00-the-problem.md),
[04](04-learned-not-decorative.md).

### Acceptance test
The behavioral benchmark: build the model, **delete the substrate, disable
grep/embeddings**, give a task; pass iff the agent answers from beliefs and
re-fetches cited slices to verify. See [11](11-acceptance-test.md).

### Context footprint
The thing memory-bounding actually bounds — what gets injected into the agent's
context, *not* disk. Scales with task blast radius, not model size. See
[05](05-memory-bounding.md).

### Lazy creation / lazy re-verification
Build beliefs as tasks touch areas (no cold-start batch inference); mark stale
cheaply on diff but only pay the LLM re-verify when a task *consults* the belief.
See [05](05-memory-bounding.md).

### Drift = eviction
Drift handling and eviction are one mechanism: stale + never-consulted → evicted
free; stale + in-demand → re-verified. See [05](05-memory-bounding.md).

### Supersession
Beliefs replace rather than append. Re-confirmation raises confidence *in place*;
refinement replaces and inherits provenance. You store "what survived contact with
evidence," not the evidence. See [05](05-memory-bounding.md).

### Substrate loop
The parser-bound, `O(Δ)`, background loop that keeps the substrate at HEAD. Cheap.
Lift from salsa. See [07](07-complexity-targets.md), [09](09-prior-art.md).

### Belief loop
The LLM-bound, lazy, consultation-triggered loop. Expensive. **Not** a background
sync — never "keep beliefs fresh on every commit." Shares storage with the
substrate loop and nothing else. See [07](07-complexity-targets.md).

### Blast radius
The set of dependents an edit affects, queryable from the dependency graph
*before* paying the update cost. Why steady-state update is `O(Δ + affected
dependents)`. See [07](07-complexity-targets.md).

### `salsa`
rust-analyzer's incremental, query-based, memoized computation framework. The
reference implementation of the substrate loop — lift wholesale. Has **no** answer
for the belief/invariant layer. See [09](09-prior-art.md).

### SCIP / LSIF
Standardized cross-reference index formats. Reference for serializing
symbol/reference graphs portably. See [09](09-prior-art.md).

### WAL (write-ahead logging)
SQLite mode giving concurrent readers + a single writer. The indexer daemon owns
all writes; the agent only reads — which kills the read/write race. See
[06](06-storage-runtime.md).

### Indexer daemon
The only "always-running" component: event-driven (git hooks + Tether edit
hooks), single-writer, keeps the substrate at HEAD, keys the index by commit hash.
SQLite itself is passive. See [06](06-storage-runtime.md).
