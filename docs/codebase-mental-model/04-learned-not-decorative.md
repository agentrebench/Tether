# 04 — Learned, Not Decorative

> "You should keep what you learned even if you throw the book away."

The test of whether the model has *learned* anything is whether its conclusions
survive the destruction of the material they were derived from. This file makes
that precise and turns it into a runnable benchmark.

## The book analogy

You read a library of books and form conclusions. Later, the library burns down.
If your conclusions were real knowledge, you still have them — and you can go find
*another* copy of the relevant page to double-check any one of them. If your only
"knowledge" was "the answer is on page 200 of the book that just burned," you have
nothing.

Map it onto the system:

- **The library** = the derived substrate (call graph, dep graph, symbol tables).
  Regenerable from the repo. **Disposable by design.**
- **The conclusions** = the inferred beliefs. What we want to keep.
- **The trap:** a belief justified by a pointer *into the substrate* — "true
  because of the call graph I built last Tuesday" — becomes **unfalsifiable** the
  moment the substrate is regenerated or deleted. You can no longer check it. It's
  the burned page.

## The fix: the citation survives the toss, not the substrate

A belief's evidence pointer is **content-addressed into the repo**, not into the
substrate:

```
billing/refunds.py @ def RefundService.refund @ commit abc123
```

This survives throwing the substrate away, because it addresses *the repo*, which
is still there (git is the history). So any belief's evidence can be, on demand:

1. **re-fetched** — re-parse exactly that one slice (`refunds.py`'s
   `RefundService.refund` at `abc123`),
2. **verified** — does the belief still hold against that freshly-parsed slice?
3. **tossed again** — drop the substrate; keep the (re-confirmed) belief.

The substrate is scaffolding you can rebuild any time you need to check your work.
The belief plus its content-addressed citation is the durable artifact.

## The three criteria for "learned" (restated, operational)

A belief counts as **learned** iff:

1. **Stated in its own terms** — `(owns billing refunds)`, a claim you can read
   without the substrate present.
2. **Re-derivable from its content-addressed citation** — point (1)–(3) above
   work: fetch the cited slice, re-check, done.
3. **Load-bearing** — it is actually consulted and it *changes agent behavior*:
   it gates a plan, vetoes an edit, or scopes retrieval. A true-but-never-read
   belief is decorative.

Miss any one and it's decoration. Decoration is net-negative: it spends context
budget and manufactures false confidence.

## The acceptance test (behavioral, runnable)

This is how "did it learn?" stops being a vibe and becomes a green/red bar:

> 1. Build the model over a repo as the agent works some tasks.
> 2. **Delete the entire derived substrate.**
> 3. **Disable grep and embeddings** (take away the agent's ability to
>    reconstruct understanding from scratch).
> 4. Give the agent a task that requires understanding, e.g. *"what does changing
>    `RefundService.refund` affect?"*, *"is constructing a singleton cache allowed
>    here?"*, *"what owns refunds?"*

**Pass:** it answers from retained beliefs, then re-fetches *only the specific
cited slices* to verify before editing.

**Fail:** it falls back to reconstructing its understanding from the repo — which
is now impossible — and stalls. The beliefs were decorative.

This test is itself a benchmark. It is also the **forcing function** for the whole
design: a belief that can't pass it isn't worth storing. Build the harness early
(see [10 — Roadmap](10-roadmap.md)); full spec in
[11 — Acceptance test](11-acceptance-test.md).

## Why content-addressing by commit matters elsewhere too

The same `@ commit` addressing that makes beliefs re-checkable also:

- **drives invalidation** — when a diff touches `refunds.py`, every belief citing
  a slice of `refunds.py` is cheaply marked *potentially stale* (see
  [05](05-memory-bounding.md)),
- **keys the substrate cache by commit hash** — a branch switch becomes "is this
  commit's slice already indexed?" rather than "recompute everything" (see
  [06](06-storage-runtime.md)).

Content-addressing is the single mechanism that ties together
*re-verifiability*, *invalidation*, and *branch-aware caching*. It is worth
getting exactly right.

## One-line summary

Keep the conclusion and its content-addressed citation; throw the substrate away;
prove you learned by surviving its deletion. If a belief can't survive that, it
was never knowledge.
