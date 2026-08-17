# 07 — Complexity Targets

> State the complexity contracts precisely. An O(n)-per-edit update loop *is* the
> failure mode — it means paying the full reconstruct cost on every change, which
> is the amnesia this system exists to cure.

## The four contracts

| Operation | Target | Notes |
|---|---|---|
| **Cold build** | `O(n)` in repo size | One-time, unavoidable, fine. You parse the whole repo once. |
| **Steady-state substrate update** | `O(Δ)` | Proportional to *what changed*, **not** `O(repo)`. This is the contract that matters most. |
| **Query / retrieval** | `O(log n + k)` | Sublinear in store size (B-tree indexes), `k` = result size. This is why total model size is irrelevant to context footprint. |
| **Belief layer** | not an index-complexity problem | Bounded by **number of LLM calls**, which lazy verification minimizes. No cheap big-O. |

## Cold build — `O(n)`, and that's fine

The first time you index a repo, you parse all of it. Linear in repo size,
one-time, unavoidable. Don't contort the design to avoid it; do make it
*resumable and commit-keyed* so you never pay it twice for the same commit (see
[06](06-storage-runtime.md)).

## Steady-state update — `O(Δ)`, the contract that defines success

After the cold build, an edit must cost **proportional to what changed**, not to
the size of the repo. If your update loop walks the whole repo on every edit, you
have rebuilt the amnesia loop with extra steps — you pay reconstruction cost
continuously. That is the single most important thing to get right, and it is
exactly what salsa-style incremental invalidation gives you (see
[09](09-prior-art.md)).

### The honest caveat: `O(Δ + affected dependents)`

`O(Δ)` is slightly too clean. The real contract is **`O(Δ + affected
dependents)`**. An edit to a widely-depended-on core type genuinely changes
information *downstream* of it — every dependent's type resolution may shift — so
that change can approach `O(n)` *for that particular edit*. This is not a failure;
the information really did change that widely.

The crucial property: **the dependency graph tells you the blast radius before you
pay it.** You can answer "how much does this edit affect?" by querying the graph,
*then* decide. The cost is proportional to the true downstream impact, and it is
*predictable in advance* rather than a surprise. A typo fix is `O(Δ)`; renaming a
field on a universally-imported base class is legitimately expensive, and the
graph told you so up front.

## Query / retrieval — `O(log n + k)`, why size stops mattering

With B-tree indexes on the node/edge/belief tables, a lookup is `O(log n)` to find
the entry point plus `O(k)` to read out the `k` results. Sublinear in the store
size `n`. This is the formal reason behind
[memory bounding](05-memory-bounding.md)'s claim that a 10 GB and a 100 MB model
produce the same injection: retrieval cost depends on the *result* size and the
*log* of the store, not the store itself. Grow the store all you like; the prompt
stays small.

## Belief layer — measured in LLM calls, not big-O

The belief layer has no meaningful index complexity because its cost is *not*
computational — it's **the number of LLM calls** it makes. There is no clever data
structure that makes inference cheap. The only lever is **doing less of it**, which
is precisely what [lazy creation + lazy re-verification](05-memory-bounding.md)
delivers: infer only where work happens, re-verify only what a task consults.

So when reasoning about belief-layer cost, count LLM calls, not operations. "Is
this a background sync that re-verifies everything on every commit?" — if yes, the
cost is unbounded and the design has failed. It must be lazy and
consultation-triggered.

## The two loops (share storage, nothing else)

The system is two loops with deliberately different cost models:

| | Substrate loop | Belief loop |
|---|---|---|
| Bound by | parser, `O(Δ)` | LLM calls, lazy |
| When it runs | every edit / commit (background) | when a task *consults* a belief |
| Cost | cheap | expensive |
| Trigger | git + edit hooks | retrieval / consultation |

**They share storage (SQLite) and nothing else.** The cardinal sin is letting the
belief loop ride the substrate loop's trigger — "keep beliefs fresh on every
commit." That reintroduces per-commit LLM cost, i.e. *the exact cost the whole
design removes*. The substrate loop is allowed to be eager because it's cheap; the
belief loop must stay lazy because it's not.

## One-line summary

`O(n)` once, `O(Δ + dependents)` per edit (and the graph quotes the price first),
`O(log n + k)` to query, and the belief layer counts LLM calls — kept minimal by
laziness. Two loops, shared storage only.
