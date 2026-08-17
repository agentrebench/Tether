# 05 — Memory Bounding

> The store grows with **active surface area**, not with history or lines of code.
> The thing to bound is **context-window footprint**, not disk.

## Bound the right thing

Disk is cheap. A multi-gigabyte SQLite file is completely fine. So the goal is
*not* "keep the database small." The goal is to keep **what gets injected into the
agent's context** small and relevant.

The mechanism: **load the architecture index first, drill into touched regions
only.** Context footprint scales with the **blast radius of the task**, not with
the size of the model. A 10 GB model and a 100 MB model produce the *same* ~2 KB
injection for the same task, because retrieval is bounded — you pull the
architecture index plus the slices the task touches, nothing more. (This is what
the [sublinear query complexity](07-complexity-targets.md) buys you.)

Internalize this inversion: **total model size is irrelevant to context
footprint.** Stop optimizing disk; optimize what crosses into the prompt.

## Retention tiered by regenerability

Different data has different cost-to-recreate, so it gets different retention:

| Tier | What | Retention policy | Why |
|---|---|---|---|
| Derived substrate | call/dep/data-flow graphs, symbols | **HEAD only**, replaced incrementally; never versioned across history | git is the history; re-runnable; zero temporal growth |
| Traces / coverage | runtime edge frequencies | keep **decayed aggregates**, discard raw runs | the one truly append-only input — aggregate or it grows forever |
| Descriptive beliefs | ownership, "how it works" | a **cache** — evict under pressure; cap the count | evicting costs a re-inference, not lost data |
| Decisions / rejected patterns / confirmed invariants | the durable core | **never auto-evict**; archive (don't delete) when the module is gone | intrinsically tiny; irreplaceable intent |

Two things worth dwelling on:

- **Descriptive beliefs are a cache, not a ledger.** Evicting one is cheap — it
  costs a future re-inference if it's ever needed again, nothing more. So evict
  on **consultation frequency** (LRU), with staleness + low confidence as
  tiebreakers, and **cap the count**. LRU is near-optimal here: the working set
  is skewed (work clusters in a few areas), so recency-of-use beats any clever
  scoring scheme.
- **The durable core is tiny.** Decisions, rejected patterns, and confirmed
  invariants number in the dozens-to-hundreds *per repo over years*, ~1 KB each.
  Never auto-evict them. When their module ceases to exist, **archive** rather
  than delete — the intent ("we rejected singleton caches because hot-reload went
  stale") may matter again.

## The two mechanisms that make it flow over time

### 1. Lazy creation + lazy re-verification

**Do not batch-infer the whole repo at t=0.** A cold-start mass inference produces
*a pile of confident claims of unknown accuracy* — which is exactly the failure
mode (decorative beliefs at scale). Instead:

- **Build beliefs as tasks touch areas.** The active working set self-sizes to
  where work actually happens. Dead regions of the repo cost nothing because no
  belief is ever formed about them.
- **Mark stale cheaply on diff** (a content-addressed citation match — no LLM),
  but **only re-verify (pay the LLM call) when a task actually consults the
  belief.** A belief that goes stale and is never read again is never re-verified.

### 2. Drift handling and eviction are the same mechanism

This is the elegant part. A belief that is **stale and never consulted again**
gets **evicted without ever paying for re-verification**. The expensive re-verify
fires *only* for beliefs that are both **stale and in demand**. Drift and eviction
are not two systems; they're one:

```
stale + never read again      → evicted (free)
stale + a task wants it        → re-verify (LLM call), then keep or demote
fresh + read                   → confidence rises in place
```

You never pay to keep dead knowledge fresh, and you never throw away knowledge a
task is actively leaning on.

## Supersession, not accumulation

Beliefs **supersede**; they do not append. There is no event log of every time a
belief was touched.

- A **re-confirmed** belief raises its confidence **in place** — no new row, no
  append.
- A **refined** belief **replaces** the old one and **inherits its provenance**.

So confidence is `accumulated confirmation count + verification history`, stored
*without* keeping the substrate snapshots that produced each confirmation. You
store **what survived contact with evidence**, not the evidence itself. (The
evidence is always re-fetchable via the content-addressed citation — see
[04](04-learned-not-decorative.md).)

This is what keeps the belief layer from becoming the append-only ledger the whole
design is trying to avoid.

## A worked picture

Repo of 2M LOC. The team spends a sprint in `billing/`. Over that sprint:

- Substrate stays HEAD-only; each commit updates O(Δ) of the graphs.
- ~40 descriptive beliefs form about `billing/` as tasks touch it; older beliefs
  about an untouched `legacy/reports/` corner were never created or were evicted
  by LRU.
- Three invariants get confirmed (mined from PR comments) and compiled.
- A task about refunds injects: the architecture index (~1 KB) + the ~6 beliefs
  and 1 invariant whose citations fall in the refund blast radius. Total
  injection: a couple KB — independent of the 2M LOC behind it.

## One-line summary

Bound context footprint, not disk; retain by regenerability; build and verify
beliefs lazily so drift-handling and eviction are one mechanism; supersede rather
than accumulate.
