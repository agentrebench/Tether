# 01 — The Architecture Split

> This split governs everything. If you internalize one structural idea from this
> directory, make it this one.

There are **two layers with opposite cost models and opposite lifecycles**, plus
a third thing that *looks* like it belongs to the first layer but does not. Keep
them strictly separated. Collapsing them is the original sin that every downstream
failure traces back to.

## Layer 1 — Derived substrate (parser-produced, regenerable, ground truth)

This is a **materialized view of HEAD**. It is everything a parser and static
analyzer can produce deterministically:

```
AST → symbols / imports → name binding + types → call graph → dependency graph
```

Plus two pillars a naive version forgets:

- **Dynamic pillar** — control-flow graph, data-flow, and *aggregated* trace /
  coverage data. (Aggregated, decayed frequencies — not raw runs; see
  [05](05-memory-bounding.md).)
- **Data pillar** — schema, types, the I/O surface (what crosses the process
  boundary: HTTP, DB, files, queues).

Properties that define this layer:

- **No confidence scores.** It is re-runnable ground truth. A fact is either
  currently derivable from HEAD or it isn't. "Confidence 0.8 that `foo` calls
  `bar`" is a category error here.
- **Exactly one version is stored** — the view of HEAD. History lives in git;
  the substrate does not version itself across commits. Zero temporal growth.
- **Replaced incrementally.** When code changes, you re-derive *the changed
  slice and its affected dependents*, not the world.
- **Cost model: parser-bound, O(Δ).** Updates cost proportional to what changed.
  See [07 — Complexity targets](07-complexity-targets.md).

Because it is regenerable, the substrate is **disposable**. You can delete the
whole thing and rebuild it from the repo. That disposability is load-bearing for
the [acceptance test](04-learned-not-decorative.md) and for memory bounding.

## Layer 2 — Inferred layer (LLM-produced, not regenerable, where hallucination lives)

This is the conclusions an LLM draws *about* the substrate and the code:

- **Modules** — the conceptual grouping of code that no parser hands you.
- **Ownership** — `billing owns refunds`.
- **Descriptive beliefs** — claims about how things work and why.
- **Decisions** — what was chosen and what was rejected, and why.
- **Invariants** — the rules the architecture is supposed to obey.

Properties:

- **Carries confidence** and is **defeasible** (most of it — see the crucial
  exception for prescriptive beliefs in [02](02-belief-types.md)).
- **Not regenerable for free.** Re-creating a belief costs an LLM call. So this
  layer is a **cache**, built lazily, evicted under pressure — never a complete
  materialization.
- **Cost model: LLM-call-bound, lazy.** There is no cheap big-O for this layer;
  its cost is *the number of LLM calls*, which lazy verification exists to
  minimize. See [05](05-memory-bounding.md) and [07](07-complexity-targets.md).

This is the **novel and defensible** part. The substrate is well-trodden; the
inferred layer is where the research risk and the product value both concentrate.

## The thing in between — the architecture graph (recovered + interpreted)

There is a strong temptation to treat the architecture graph — the
module-to-module wiring of the system — as just one more parser output. **Resist
it.** The architecture graph needs edges the substrate *cannot see*:

- an HTTP client calling a service whose handler is defined in another package,
- a message pushed to a queue and consumed elsewhere,
- a SQL string that implies a read/write dependency on a table,
- dependency-injection wiring that connects an interface to its implementation at
  runtime.

None of those are visible to "follow the call edges in the AST." They are
**recovered** (you have to reconstruct the edge from indirect signal) and
**interpreted** (you have to decide what the signal means). So the architecture
graph straddles the two layers: it is built *on* the substrate but is *not
derived from it* in the clean, deterministic sense Layer 1 demands. Give it its
own treatment — part heuristic recovery, part inferred belief — rather than
pretending a parser produced it.

## Why the strict separation matters

Three failures all come from blurring the layers:

1. **Confidence pollution.** If you let confidence scores leak onto derived
   facts, you stop trusting ground truth and start "voting" on what the code
   says. The code is not a democracy; it is the code.
2. **Cost-model confusion.** If beliefs ride the substrate's O(Δ) update loop,
   you end up re-inferring beliefs on every commit — reintroducing exactly the
   per-edit LLM cost the whole design exists to remove. The two loops must share
   storage and *nothing else* (see [07](07-complexity-targets.md)).
3. **Unfalsifiable beliefs.** If a belief's only justification is a pointer into
   the substrate, deleting the substrate orphans the belief — it can no longer be
   checked. The fix (content-addressed citations into the *repo*) only works
   because the substrate is treated as disposable, not as the belief's home. See
   [04](04-learned-not-decorative.md).

## One-line summary

The substrate is a regenerable view of HEAD with no opinions; the inferred layer
is a lazy cache of opinions that must justify themselves against the repo; the
architecture graph is recovered glue between them and deserves its own handling.
Keep their cost models apart.
