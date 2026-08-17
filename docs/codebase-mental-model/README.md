# Persistent Codebase Mental Model

> A persistent, queryable, incrementally-updated model of a repository that the
> agent consults *before* retrieval — so it reasons over accumulated
> understanding instead of rebuilding it from grep and embeddings every session.

> **Implementation status.** A first cut of this design is built and tested in
> [`core/codebase_model/`](../../core/codebase_model/) (see its
> [README](../../core/codebase_model/README.md) for the module map, usage, and the
> deliberate deviations from this spec). The runnable acceptance benchmark from
> [doc 11](11-acceptance-test.md) passes: build the model, delete the substrate,
> and it still answers `owns`/`affects`/`allowed` from the belief/invariant layer,
> re-fetching cited slices to verify. Opt in with `codebase_model_enabled` and
> drive it with `/persistence` in the REPL (the LLM lives at `/model`).

This directory is the design substrate for that subsystem. It is documentation,
not code: read it to build the *mental model of the mental model* before any of
it is implemented. Any model driving the Tether harness — today's or a future
one — should be able to read these files and understand not just *what* to build
but *why each boundary is drawn where it is*, because every boundary here exists
to defend against a specific failure mode.

## The one through-line

**Maximize the fraction of the model that is *checkable*; minimize the fraction
that is merely *asserted*.**

Every design decision in this directory serves that single sentence. Compiled
invariants, content-addressed re-fetch, the behavioral acceptance test, the
strict split between derived and inferred layers — all of it is in service of
pushing belief out of "the LLM said so at confidence 0.9" and into "a
deterministic query returned a counterexample with a file and a line." When in
doubt about a design choice, ask: *does this make more of the model checkable?*

## The problem in one paragraph

Coding agents reconstruct their understanding of a repo from grep and embeddings
at the start of every session, and forget it at exit. They are perpetually
amnesiac. The same questions — *what owns refunds, what does this change affect,
is this pattern allowed here* — get re-derived from scratch, expensively, and
inconsistently. We want to keep the **conclusions** an agent reaches while
working, make them **persistent**, keep them **honest** as the code drifts
underneath them, and **bound their growth** so the store scales with the active
surface area of work, not with history or lines of code.

## The two-layer split (read this before anything else)

The whole architecture is governed by one split. Two layers with **opposite
cost models and opposite lifecycles**, kept strictly separate:

| | Derived substrate | Inferred layer |
|---|---|---|
| Produced by | parsers / static analysis | LLM |
| Regenerable? | yes — it's a view of HEAD | no — re-inference costs an LLM call |
| Confidence scores? | none — it's ground truth | yes — defeasible beliefs |
| Cost model | parser-bound, O(Δ) | LLM-call-bound, lazy |
| Versioning | HEAD only, replaced in place | a cache, evicted under pressure |
| Where bugs hide | nowhere (re-runnable) | here (hallucination lives here) |

The substrate is well-trodden engineering (see [prior art](09-prior-art.md):
salsa, SCIP). The inferred layer is the novel, defensible part — and the part
with no off-the-shelf solution. Most of the risk and most of the value live
there. See [01 — the architecture split](01-architecture-split.md).

## How to read this directory

Read in order the first time; the early files establish vocabulary the later
ones lean on.

1. [00 — The problem & what "learned" means](00-the-problem.md) — the amnesia
   problem, and the precise, testable definition of a belief that was actually
   *learned* vs. one that's decorative.
2. [01 — The architecture split](01-architecture-split.md) — derived substrate,
   inferred layer, and the recovered architecture-graph that sits between them.
3. [02 — Belief types](02-belief-types.md) — derived / descriptive /
   prescriptive, and why conflict resolution **must** branch on the type. The
   single most important distinction in the design.
4. [03 — The invariant compiler](03-invariant-compiler.md) — compiling
   constraints down to deterministic graph queries that return counterexamples;
   asymmetric enforcement gating.
5. [04 — Learned, not decorative](04-learned-not-decorative.md) — the book
   analogy, content-addressed evidence, and the behavioral acceptance test.
6. [05 — Memory bounding](05-memory-bounding.md) — bounding context footprint
   (not disk); retention tiers; lazy creation and re-verification; supersession.
7. [06 — Storage & runtime](06-storage-runtime.md) — SQLite as source of truth,
   Lisp as query surface, the event-driven indexer daemon.
8. [07 — Complexity targets](07-complexity-targets.md) — the big-O contracts and
   the two loops that share storage and nothing else.
9. [08 — Representation](08-representation.md) — the target s-expression idiom,
   with worked examples of each belief type.
10. [09 — Prior art](09-prior-art.md) — what to lift wholesale (salsa) and what
    has no precedent (the belief/invariant layer).
11. [10 — Roadmap](10-roadmap.md) — build order: de-risk the substrate first,
    build the acceptance harness early.
12. [Glossary](glossary.md) — every load-bearing term, in one place.

## The acceptance test, up front

Because "did it actually learn?" is otherwise a judgment call, the design pins it
to a runnable behavioral test. Build the model, **delete the entire derived
substrate, disable grep and embeddings**, and give the agent a task. If it can
still answer *what does this affect / is this pattern allowed / what owns
refunds* — and then re-fetch only the specific cited code slices to verify before
editing — it **learned**. If it falls back to reconstructing understanding from
the repo, the beliefs were decorative. This test is itself a benchmark, and it is
built early, not bolted on at the end. See
[04](04-learned-not-decorative.md) and [11](11-acceptance-test.md).

## What this is not

- It is not a code-search index with extra steps. Retrieval is the *fallback*;
  the model is consulted first.
- It is not a per-repo, hand-tuned knowledge base. It must be **generic** — no
  per-repo hardcoding — and must **flow correctly over time** as code evolves.
- It is not a frozen snapshot. It tracks drift, supersedes stale beliefs, and
  bounds its own growth without an ever-growing ledger.
