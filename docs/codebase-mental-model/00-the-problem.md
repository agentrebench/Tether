# 00 — The Problem & What "Learned" Means

## The amnesia loop

A coding agent today works like this, every single session:

1. Get a task.
2. grep / embed / read its way to a *working theory* of the relevant slice of
   the repo — who calls what, what owns what, what's allowed here.
3. Do the task.
4. Exit. **The theory evaporates.**

Next session, on a related task, it pays for that theory again from zero. The
reconstruction is expensive (many tool calls), inconsistent (a different walk of
the code yields a different theory), and — worst — it never compounds. An agent
that has worked in a repo for six months knows no more on Monday than it did on
day one. It is perpetually a first-day contractor.

The fix is not "remember more text." Dumping transcripts into a vector store
just moves the reconstruction cost around and adds a new failure mode: confident
recall of stale claims. The fix is to keep the **conclusions** — structured,
queryable, honest about their own staleness — and to make consulting them cheaper
and more trustworthy than re-deriving them.

## Three properties the model must have

Stated as constraints because each one kills a tempting-but-wrong design:

- **Generic.** No per-repo hardcoding. The same machinery models a Django
  monolith, a Rust workspace, and a polyglot microservice mesh. (Kills:
  hand-authored per-repo ontologies.)
- **Flows correctly over time.** Code evolves; the model must track drift and
  demote or evict what no longer holds — without a human curating it. (Kills:
  the static one-shot "index the repo once" snapshot.)
- **Bounded growth.** Storage footprint — specifically *context-window
  footprint* — scales with the **active surface area of work**, not with repo
  size or project history. (Kills: the append-only ledger that grows forever.)

## What "learned" means — precisely

This is the definitional heart of the project. We need a test that distinguishes
a belief the agent *actually learned* from one that merely *sounds learned*. A
belief counts as **learned** if and only if all three hold:

1. **Stated in its own terms.** The belief is a standalone claim —
   `billing owns refunds` — not a pointer like "see the call graph I built
   last Tuesday." A belief whose only justification is a pointer to a now-deleted
   artifact is an *unfalsifiable* claim. (See the book analogy in
   [04](04-learned-not-decorative.md).)
2. **Re-derivable from its content-addressed citation.** The belief carries
   evidence pointers addressed *into the repo itself* — e.g.
   `billing/refunds.py @ RefundService.refund @ commit abc123`. The substrate
   that originally produced it may be long gone; the citation lets you re-fetch
   exactly that slice, re-parse it, and check the belief still holds.
3. **Load-bearing.** The belief is *actually consulted* and *changes agent
   behavior* — it gates a plan, vetoes an edit, or scopes retrieval. A belief
   that no task ever reads is decorative regardless of how true it is.

A belief that satisfies all three is knowledge. A belief that fails any one of
them is decoration, and decoration is worse than nothing: it occupies context
budget and lends false confidence.

## The behavioral acceptance test (the whole project, in one experiment)

Definitions are cheap; here is the runnable one. It is the project's north star
and its benchmark:

> Build the model over a repo. Then **delete the entire derived substrate**
> (call graph, dep graph, symbol tables — everything regenerable) and **disable
> grep and embeddings**. Hand the agent a real task. Watch what it does.
>
> - **Pass:** it answers "what does this affect / is this pattern allowed / what
>   owns X" from its retained beliefs, then re-fetches *only the specific cited
>   slices* to verify before it edits.
> - **Fail:** it falls back to reconstructing its understanding from the repo —
>   which we just took away — and stalls or flails. The beliefs were decorative.

Build this harness **early** (see [10 — Roadmap](10-roadmap.md)). "Did it learn?"
should be a green or red bar from week one, not a debate at the end. Full spec in
[11 — Acceptance test](11-acceptance-test.md).

## Why this is hard (and worth doing)

The substrate half — parse to symbols, bind names, build a call graph, keep it
incremental — is solved engineering. You can lift it wholesale (see
[09](09-prior-art.md)). The hard, unsolved, defensible half is everything that
hangs off the inferred layer: representing beliefs so they can be *checked* and
not just *trusted*, resolving conflicts between belief and code *correctly* (which
means differently for different kinds of belief — see
[02](02-belief-types.md)), and bounding the whole thing so it stays small and
honest as the repo churns underneath it for years.
