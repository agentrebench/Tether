# 03 — The Invariant Compiler

> The highest-leverage piece. Wherever possible, **compile an invariant down to a
> deterministic graph query that runs on every diff and returns a
> counterexample** — instead of a soft assertion the LLM eyeballs at confidence
> 0.9.

This is where the [through-line](README.md#the-one-through-line) — *maximize the
checkable, minimize the asserted* — does its heaviest lifting. An invariant that
stays a natural-language belief is only as reliable as the LLM that re-reads it.
An invariant compiled to a query is reliable as arithmetic: it either finds a
counterexample or it doesn't, and when it does, it hands you a location.

## From soft belief to hard check

The move is to express the rule as a query over the substrate's graphs
(call graph, data-flow graph, dependency graph, object lifetimes) such that a
violation is a *graph fact*, not an opinion:

| Invariant (English) | Compiles to | Substrate it runs on |
|---|---|---|
| "all writes go through `UnitOfWork`" | domination check — every write node is dominated by a `UnitOfWork` frame | call graph + data-flow |
| "plugins can't call `Renderer` directly" | forbidden-edge query — no edge from any `plugins/*` node to `Renderer` | call graph |
| "the cache is scoped, not global" | object-lifetime check — no construction of `Cache` at module/global scope | object lifetime |

The output is not a belief. It is a **pass/failed check with a location**:

```
FAILED writes-through-uow:
  RefundService.retry writes to persistence at billing/refunds.py:88,
  outside any UnitOfWork frame.
```

That is a counterexample an agent (or a human) can act on immediately — and it is
*checkable* by anyone, no model required.

## What stays soft

Not every invariant compiles. Genuinely-semantic rules — "error messages should
not leak PII," "this retry must be idempotent" — may resist a deterministic
encoding. Those **stay as soft, LLM-checked beliefs**, and that's fine. The
design goal is to *push as many as possible* into the deterministic layer, not to
pretend everything fits. The fraction you compile is the fraction you can trust
without a model in the loop.

## Asymmetric enforcement — the part people get wrong

Enforcement must gate on confidence **asymmetrically**, because the two failure
modes are not equally bad:

- A **missing** invariant fails to catch a bug. Cost: a bug slips through, same
  as today.
- A **false** invariant vetoes a *correct* edit. Cost: the system actively
  obstructs good work and trains the user to ignore it.

A false invariant is strictly worse than a missing one. Therefore:

- **Hard-block** (veto the edit) **only** on invariants that are
  *compiled-and-checked* or *human-confirmed*. These are the ones you trust
  enough to stop work over.
- **Soft-warn** (flag, don't block) on *inferred* invariants — the LLM's
  guesses. They're worth surfacing as candidates, never worth vetoing on.

Stated as a slogan: **be conservative about asserting invariants, liberal about
flagging candidates.** It is fine to surface "this might violate a pattern"; it
is not fine to block an edit on a rule no human ever confirmed.

```lisp
(invariant writes-through-uow
  :enforcement :hard         ; compiled + confirmed → may veto
  :confidence :confirmed)

(invariant retries-are-idempotent
  :enforcement :soft         ; inferred, semantic → warn only
  :confidence 0.7)
```

## Capture: mine intent, don't infer from snapshots

A compiled check is only as good as the invariant behind it, and the most common
way to get a *false* invariant is to infer it from a snapshot: "every write in the
current tree goes through `UnitOfWork`, therefore that's a rule." It might be a
coincidence, not an intent. So invariant **capture** should draw on signals of
intent:

- PR comments where a reviewer corrected someone,
- commit messages that explain *why* a change was made,
- design docs / ADRs,
- and a **human confirmation step** before an invariant earns `:hard`
  enforcement.

Current code satisfying a rule ≠ the rule being intended. Treat inferred
invariants as *candidates* that get promoted to confirmed only via the deterministic
compiler **and/or** a human saying "yes, that's a rule."

## The lifecycle of an invariant

1. **Candidate** — surfaced by inference or by mining PRs/commits/docs.
   `:enforcement :soft`. Warns only.
2. **Compiled** — expressed as a graph query that returns counterexamples.
   Runs on every diff. Now *checkable*.
3. **Confirmed** — compiler + human agree it's a real rule. `:enforcement :hard`.
   May veto edits, always with a counterexample attached.
4. **Violated** — a diff produces a counterexample. This is a **finding**, not a
   reason to weaken the rule (see [02](02-belief-types.md): prescriptive beliefs
   `:raise`, never `:mark-stale`).

## One-line summary

Compile invariants into deterministic queries that emit counterexamples with
locations; hard-block only on compiled/confirmed ones; mine intent for capture.
The compiled fraction is the trustworthy fraction.
