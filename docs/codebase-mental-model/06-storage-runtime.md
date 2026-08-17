# 06 — Storage & Runtime

> **SQLite as the single source of truth; Lisp as the query + reasoning surface.**
> Do **not** dual-write a live Lisp model and a database.

## The Datomic lesson (and the anti-pattern it warns against)

Datomic's design is the right shape: *datoms live in the store; Datalog runs over
them.* One source of truth, a logic language as the query surface on top. The
anti-pattern is the opposite: keeping an authoritative in-memory model *and* a
database and trying to keep both in sync. That dual-write split is a bug factory —
two copies of the truth that drift, with no principled answer to "which one is
right."

So: **SQLite holds durable truth. Lisp is invoked per-task to load a slice and
reason over it. Lisp is the query surface, never the daemon.**

## What lives in SQLite (indexed rows)

Everything is rows in an indexed relational store:

- **Node table** — symbols, functions, modules, types (the graph's vertices).
- **Edge table** — calls, imports, data-flow, dependency edges (the graph's
  edges).
- **Belief rows** — descriptive beliefs, with confidence, kind, `:on-conflict`
  policy, verification history.
- **Evidence pointers** — the content-addressed citations
  (`file @ symbol @ commit`) that justify beliefs and drive invalidation.
- **Invariant definitions** — the check, stored as a blob (the compiled query or
  the soft spec).
- **Invariant results** — the *materialized* result of running a check: pass /
  failed, the counterexample, and when it was run. These are rows so you can query
  "what's currently violated" without re-running anything.

B-tree indexes on these tables give [sublinear lookups](07-complexity-targets.md)
— `O(log n + k)` — which is what makes total store size irrelevant to retrieval
cost.

## What Lisp does (per task, not always-on)

For each task:

1. **Project** the relevant rows up into s-expressions (the
   [representation](08-representation.md) idiom).
2. **Run logic queries** over them — `rejected-pattern-p`, `module-invariants`,
   `affected-modules-for-task`, the compiled invariant checks.
3. **Write results back** as rows (e.g. a new invariant-result row, a bumped
   confidence).

A reader/printer maps **rows ↔ s-exprs** at the boundary. The s-expression form
exists only transiently, in the per-task Lisp invocation. It is *not* a second
persistent model.

### Why Lisp is not a daemon

A standing live Lisp image holding the model in memory is explicitly rejected:

- **In-memory = a crash loses the model.** Durability must come from the file.
- **Concurrency gets ugly.** A stateful long-lived process invites races between
  readers and the writer.

SQLite is *passive* — it's a file plus a library, nothing "runs." That passivity
is a feature: the truth sits on disk, and Lisp is a transient consumer of it.

## "Always running" = an event-driven indexer daemon

The only thing that genuinely runs is a thin **indexer daemon** that keeps the
substrate current. Its design:

- **Trigger on events, not polling.** Two event sources:
  - **git hooks** — `post-commit`, `post-checkout`, `post-merge`.
  - **edit hooks inside the Tether harness** — the agent's *own* edits are the
    tightest possible change signal; the daemon learns about a change the instant
    the agent makes it, before any commit.
- **WAL mode** — SQLite's write-ahead logging gives **concurrent readers + a
  single writer**. The **daemon owns all writes; the agent only reads.** That
  single-writer rule *kills the race* the live-image design would have created.
- **Key the index by commit hash** — the same content-addressing used for
  [evidence pointers](04-learned-not-decorative.md). A branch switch becomes a
  cache question — *"is this commit's slice already indexed?"* — not a "recompute
  the world" event.

## The clean division of responsibility

```
            ┌──────────────────────────────────────────┐
            │              SQLite (truth)               │
            │  nodes · edges · beliefs · evidence ·     │
            │  invariant defs · invariant results       │
            └──────────────────────────────────────────┘
              ▲ writes (single)            ▲ reads (many)
              │                            │
   ┌──────────────────────┐      ┌────────────────────────┐
   │  Indexer daemon       │      │  Per-task Lisp invocation │
   │  - git + edit hooks   │      │  - project rows → s-exprs │
   │  - keeps substrate    │      │  - run logic queries      │
   │    at HEAD, O(Δ)      │      │  - write results back     │
   │  - WAL single writer  │      │  (transient, not a daemon)│
   └──────────────────────┘      └────────────────────────┘
```

The daemon and the per-task reasoner **share storage and nothing else** — which
is exactly the [two-loops](07-complexity-targets.md) discipline applied at the
runtime level.

## One-line summary

One source of truth (SQLite, passive, WAL, single-writer daemon); Lisp is a
transient per-task query surface that projects rows to s-exprs and back. No live
image, no dual-write.
