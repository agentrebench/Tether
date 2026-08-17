# 02 — Belief Types

> The single most important distinction in the design. Conflict resolution
> **branches on the belief type**. A system with one update rule for all beliefs
> launders every architectural violation into "the model must be stale" — which
> is the exact failure this system exists to prevent.

There are three kinds of belief. They differ in *what happens when the belief and
the code disagree*. Do not collapse them into a single "fact" form with a single
confidence number; the type **is** the conflict-resolution policy.

## 1. Derived facts — *defer to code, always*

```lisp
(fact (calls refund-service.retry unit-of-work.commit)
  :source :callgraph :at "abc123f")
```

- **What it is:** a ground-truth statement produced by the substrate —
  `A calls B`, `module X imports Y`, `this function writes to table T`.
- **Confidence:** none. It is re-runnable.
- **On conflict with code:** there is no conflict — the code *is* the source. If
  the parse says otherwise than your stored fact, the stored fact was stale;
  **refresh it by re-parsing.** No judgment, no LLM.

## 2. Descriptive inferred beliefs — *defeasible, code wins*

```lisp
(belief billing-owns-refunds
  :kind :descriptive
  :claim (owns billing refunds)
  :confidence 0.94
  :on-conflict :demote)
```

- **What it is:** an LLM's claim *about* the system — ownership, responsibility,
  "this cache exists to avoid N+1 queries," "module A is the public API surface."
- **Confidence:** yes, and it is **defeasible** — held subject to evidence.
- **On conflict with code:** **demote.** The code wins. If the evidence no longer
  supports `billing owns refunds`, lower its confidence or evict it. The belief
  is a convenience that summarizes the code; when they disagree, the code is
  right and the belief is out of date.

## 3. Prescriptive beliefs — *challenge the code; on conflict, RAISE A VIOLATION*

```lisp
(invariant writes-through-uow
  :kind :prescriptive
  :claim (all-writes-dominated-by 'unit-of-work)
  :on-conflict :raise        ; NOT :mark-stale
  :enforcement :hard)
```

- **What it is:** invariants ("all writes go through `UnitOfWork`") and rejected
  patterns ("plugins must not call `Renderer` directly"). These are not
  *descriptions* of the code; they are *rules the code is supposed to obey*.
- **On conflict with code:** **raise a violation.** This is the inversion that is
  the entire point of the system. When the code violates "only `Auth` creates
  sessions," you have **found a bug** — you do *not* downgrade the rule to match
  the bad code.

### Why the inversion is the whole point

Imagine a single, uniform update rule: "on any belief/code conflict, mark the
belief stale and re-verify against the code." Apply it to a prescriptive belief
and watch what happens: the moment some careless commit writes to persistence
outside a `UnitOfWork`, the rule says *the invariant is stale* and quietly
rewrites it to match the violating code. The architecture's most important
constraint has just been silently deleted by the bad change it was meant to
catch. Every new violation gets **laundered into "model out of date."**

That is precisely the failure mode this whole system exists to prevent. So the
conflict-resolution policy *must* branch on type:

| Belief type | On conflict with code | Rationale |
|---|---|---|
| Derived fact | refresh (re-parse) | code is the source |
| Descriptive | demote (code wins) | belief summarizes code |
| Prescriptive | **raise a violation** | belief *judges* code |

## The grammar of "on-conflict"

Make `:on-conflict` an explicit, first-class field on every belief, with values:

- `:refresh` — re-derive from the substrate (derived facts).
- `:demote` — lower confidence / evict (descriptive).
- `:raise` — emit a violation with a location and a counterexample; do **not**
  alter the belief (prescriptive).

Storing the policy *on the belief* (rather than in one global handler) is what
makes the three-way branch unavoidable and auditable. A reviewer can see, per
belief, what the system will do when reality disagrees with it.

## Capturing prescriptive beliefs correctly

Because a false prescriptive belief is so costly (it vetoes correct edits — see
[03](03-invariant-compiler.md)), be **conservative about asserting them**.
Crucially: *current code satisfying a rule is not evidence that the rule is
intended.* Mine prescriptive beliefs from sources that express **intent** —

- PR review comments ("don't do this, route it through the UoW"),
- commit messages explaining a fix,
- design docs and ADRs,
- **explicit human confirmation,**

— rather than from pure inference over a code snapshot. A rule the code happens to
obey today might be a coincidence; a rule a human confirmed is an intent. See
[03](03-invariant-compiler.md) for how confirmed invariants then get compiled
into deterministic checks and how enforcement gates asymmetrically on confidence.

## One-line summary

Three belief types, three conflict policies: facts defer to code, descriptions
defer to code, **prescriptions judge code**. The third is the reason the system
is more than a fancy cache.
