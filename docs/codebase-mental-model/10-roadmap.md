# 10 — Roadmap

> Two pieces of build-order advice override everything else: **start with the
> substrate loop on salsa** (de-risk the well-understood half first), and **build
> the acceptance-test harness early** so "did it learn?" is measurable from day
> one rather than a judgment call at the end.

This file proposes a phased build. It is a sequence of *de-riskings*, not a
feature checklist — each phase removes the biggest remaining unknown.

## Phase 0 — Acceptance harness first (the forcing function)

Before building the thing, build the *test* for the thing. See
[11 — Acceptance test](11-acceptance-test.md).

- A repo fixture + a set of understanding-questions ("what owns X," "what does
  editing Y affect," "is pattern Z allowed here").
- The teardown: delete the derived substrate, disable grep/embeddings.
- A pass/fail scorer.

Why first: it converts the project's central claim ("the agent learned") from a
vibe into a **green/red bar**. Every later phase is graded against it. Building it
last means discovering at the end that your beliefs were decorative.

## Phase 1 — Substrate loop (lift salsa)

Build the [derived substrate](01-architecture-split.md) as an incremental,
memoized, query-based model — **lifting `salsa` wholesale** (see
[09](09-prior-art.md)).

- AST → symbols/imports → name binding + types → call graph → dependency graph.
- Commit-keyed storage in SQLite (node/edge tables), WAL, single-writer daemon.
- Hooks: git (`post-commit`/`checkout`/`merge`) + Tether edit hooks.
- **Acceptance:** `O(Δ + dependents)` updates; the dependency graph can quote a
  blast radius before paying for it.

Why here: it's the de-riskable half — known techniques, mature reference impl.
Getting it solid gives the belief layer a trustworthy foundation to cite.

## Phase 2 — Evidence pointers + content-addressing

Make every stored thing addressable as `file @ symbol @ commit`.

- Re-fetch: given a citation, re-parse exactly that slice.
- Invalidation: a diff marks citing beliefs potentially stale (cheap, no LLM).
- Branch-aware caching keyed by commit hash.

Why here: content-addressing is the spine that ties together re-verifiability,
invalidation, and caching ([04](04-learned-not-decorative.md)). Everything in the
belief layer depends on it.

## Phase 3 — Descriptive belief layer (lazy)

The first inferred layer, built **lazily** ([05](05-memory-bounding.md)).

- Beliefs created as tasks touch areas — **no cold-start batch inference.**
- Stored as cache rows with confidence, `:justified-by` citations,
  `:on-conflict :demote`.
- LRU eviction with staleness/confidence tiebreakers; capped count.
- Drift = eviction: stale + unread → evicted free; stale + consulted → re-verify.

**Acceptance:** run the Phase-0 harness. Can the agent answer "what owns X" after
the substrate is deleted? This is the first real test of *learned vs. decorative*.

## Phase 4 — Invariant compiler + prescriptive layer

The defensible core ([02](02-belief-types.md), [03](03-invariant-compiler.md)).

- Capture candidates by mining PR comments / commit messages / design docs.
- Compile what can be compiled into deterministic graph queries returning
  counterexamples; the rest stay soft.
- Three-way conflict policy enforced: facts `:refresh`, descriptive `:demote`,
  prescriptive `:raise`.
- Asymmetric enforcement: `:hard` only for compiled/confirmed; `:soft` otherwise.

**Acceptance:** a diff that violates a confirmed invariant produces a blocking
counterexample with a location; a diff that merely diverges from a descriptive
belief demotes the belief without blocking.

## Phase 5 — Architecture-graph recovery

Recover the edges the substrate can't see ([01](01-architecture-split.md)): HTTP
calls, queues, SQL strings, DI wiring. Treat as recovered + interpreted, not
derived.

Why later: it's heuristic and interpretive; it benefits from a solid substrate and
belief layer beneath it, and it's not on the critical path for proving the core
thesis.

## Phase 6 — Dynamic + data pillars at full strength

Aggregate trace/coverage into decayed edge frequencies; deepen schema / I/O
surface modeling. These enrich invariants (e.g. object-lifetime checks) and
retrieval ranking.

## The two pieces of advice, restated

1. **Substrate first (Phase 1) to de-risk the known half.** Don't get stuck
   inventing incremental analysis; import it.
2. **Acceptance harness first (Phase 0) to make learning measurable.** Don't
   defer the question "did it actually learn?" to the end, where it becomes an
   argument instead of a number.

## One-line summary

Build the test (Phase 0), then the cheap solved substrate (Phase 1–2), then the
expensive novel belief/invariant layers (Phase 3–4) graded against the test, then
the interpretive and dynamic enrichments (Phase 5–6).
