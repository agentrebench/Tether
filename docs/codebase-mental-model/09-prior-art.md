# 09 — Prior Art

> Lift the solved half wholesale. Spend your invention budget on the unsolved
> half.

## The substrate loop is solved — study and lift it

### rust-analyzer's `salsa`

`salsa` is an incremental, query-based, **memoized** computation framework. You
express your semantic model as a graph of queries; when an input changes, salsa
**invalidates and recomputes only what actually depended on that input**, reusing
memoized results for everything else. rust-analyzer keeps this live *during
editing* — every keystroke produces an `O(Δ)` update, not a reparse of the world.

This **is** the [substrate loop](07-complexity-targets.md):

- incremental, query-based, memoized → matches the `O(Δ + dependents)` contract,
- invalidation-driven → matches content-addressed staleness marking,
- kept live during edits → matches the event-driven indexer daemon.

**Study and lift it wholesale.** Do not reinvent incremental static analysis; it
is a deep, well-explored area and salsa is a mature embodiment of the right ideas.
Starting the build here also **de-risks the well-understood half first** (see
[10 — Roadmap](10-roadmap.md)).

### SCIP / LSIF

SCIP (and its predecessor LSIF) are standardized formats for **cross-reference
indexes** — "where is this symbol defined / referenced," across files and
languages. Same idea as salsa applied to the cross-reference problem, with an
interchange format. Useful as:

- a **format reference** for how to serialize symbol/reference graphs portably,
- a source of language-agnostic indexers you can feed the substrate.

## The belief / invariant layer is NOT solved — this is where the work is

Here is the load-bearing sentence: **salsa has no answer for the belief/invariant
layer.** Neither does SCIP, nor any off-the-shelf tool. Incremental static
analysis tells you *what the code is*. None of it tells you:

- what a module *owns* or is *responsible for*,
- which constraints are *intended* (vs. coincidentally satisfied),
- what was *rejected* and *why*,
- how to keep all of that **honest, bounded, and code-deferential** as the repo
  drifts for years.

That layer — its representation, its three-way conflict resolution, its lazy
verification, its supersession-not-accumulation memory model — has **no
precedent to copy.** It is simultaneously the hardest part, the part with the most
research risk, and the part where the system's defensibility lives. Everything in
this directory except the substrate loop is about that layer.

## How to split your effort

| Half | Status | Strategy |
|---|---|---|
| Substrate loop | solved (salsa, SCIP) | **lift wholesale**, don't reinvent |
| Architecture-graph recovery | partially explored (call-graph + heuristics for HTTP/queue/SQL/DI) | adapt existing recovery techniques; expect to interpret |
| Belief / invariant layer | **unsolved** | this is the invention; spend the budget here |

The temptation is to over-engineer the substrate (it's concrete and satisfying to
build) and under-invest in the belief layer (it's fuzzy and hard). Resist it. The
substrate is table stakes you can largely import; the belief layer is the reason
the project exists.

## One-line summary

Lift salsa for the substrate, reference SCIP/LSIF for cross-ref serialization, and
recognize that the belief/invariant layer has no prior art — that's the part to
actually invent.
