# 11 — The Acceptance Test (Harness Spec)

> "Did it learn?" must be a runnable green/red bar, not a judgment call. This file
> specifies the harness that produces that bar. Build it **first** (see
> [10 — Roadmap](10-roadmap.md), Phase 0).

## The core experiment

```
1. SEED      Run the agent on a series of real tasks in a fixture repo,
             letting it build beliefs as it works (lazily — no batch infer).
2. TEARDOWN  Delete the entire derived substrate (call graph, dep graph,
             symbol tables — everything regenerable).
             Disable grep and embeddings.
3. PROBE     Ask understanding-questions that require the model.
4. SCORE     Pass iff the agent answers from retained beliefs AND re-fetches
             only the cited slices to verify before acting.
```

The teardown is the whole trick: by removing *every* way to reconstruct
understanding from scratch, the only thing left for the agent to lean on is what
it actually *retained*. If it can still function, the beliefs were knowledge. If
it stalls, they were decoration.

## What the harness needs

### A fixture repo

- Non-trivial, with real architecture: modules, ownership boundaries, at least one
  enforced invariant, at least one historically-rejected pattern.
- Pinned to known commits so citations (`file @ symbol @ commit`) are stable.
- Ideally polyglot-ish or at least with one non-call-graph edge (an HTTP call or a
  queue) so [architecture-graph recovery](01-architecture-split.md) is exercised.

### A probe set (the questions)

Three question shapes, each mapping to a capability the system claims:

| Probe shape | Example | Tests |
|---|---|---|
| **Affects / blast radius** | "What does changing `RefundService.refund` affect?" | dependency reasoning from retained model |
| **Allowed / pattern** | "Is constructing a singleton `Cache` allowed here?" | prescriptive layer + rejected-pattern detectors |
| **Ownership / responsibility** | "What owns refunds?" | descriptive belief recall |

### A teardown switch

- Drop the substrate tables (or point the agent at an empty substrate DB).
- Hard-disable the grep and embedding tools for the probe phase, so a fallback to
  reconstruction *fails loudly* instead of silently rebuilding.

### A scorer

For each probe, score on three axes — all three must pass:

1. **Answered from beliefs** — the agent produced the answer from retained model
   state, not from a (now-impossible) reconstruction.
2. **Re-fetched the right slice** — before acting, it used the belief's
   content-addressed citation to re-fetch *exactly* that slice and verify, rather
   than re-reading broadly.
3. **Behavior changed** — the belief was load-bearing: it gated the plan, vetoed
   the edit, or scoped retrieval. A correct answer that didn't influence the
   action still fails criterion 3 of [learned](04-learned-not-decorative.md).

## Pass / fail, concretely

- **PASS:** "Changing `refund` affects `billing/invoicing.py` and the
  `refund.completed` queue consumer — let me re-fetch `RefundService.refund @
  abc123` to confirm the signature before I edit." (Answered from model →
  re-fetched cited slice → used it to scope the edit.)
- **FAIL:** "Let me grep for `refund`…" → grep disabled → agent stalls or
  hallucinates. The beliefs were decorative.

## Why this doubles as a benchmark

The harness is not just a gate; it's a **metric you can track over time**:

- **Coverage** — fraction of probes answerable from the model.
- **Re-fetch precision** — did it fetch *only* the cited slice, or fall back to
  broad reading?
- **Load-bearing rate** — fraction of consulted beliefs that actually changed the
  action.

Track these per build. A regression in any of them means the belief layer is
drifting toward decoration. Because it's built in Phase 0, you get this signal
from the very first belief the system forms — which is exactly when it's cheap to
correct course.

## Anti-gaming notes

- **Don't let the probe leak the answer.** Phrase probes so the answer isn't in
  the question text (else the agent "passes" by paraphrasing the prompt).
- **Verify the re-fetch is slice-scoped.** An agent that re-reads the whole file
  isn't using the citation; it's reconstructing locally. Criterion 2 must check
  the *granularity* of the fetch.
- **Rotate the fixture's HEAD.** Run probes at a commit *after* some drift, so the
  test also catches whether stale beliefs were correctly demoted/raised rather
  than confidently wrong.

## One-line summary

Seed beliefs, delete the substrate, disable retrieval, then ask: can it still
answer and verify from cited slices? Three pass-axes (answered-from-beliefs,
right-slice re-fetch, load-bearing). Build it first; track it as a benchmark.
