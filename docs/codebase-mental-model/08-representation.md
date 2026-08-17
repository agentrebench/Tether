# 08 — Representation

> The target idiom is s-expressions, projected from SQLite rows per task and
> printed back to rows after reasoning (see [06](06-storage-runtime.md)). This
> file is the worked reference for each form.

The representation is not a second persistent model — it is the **transient
working form** the per-task Lisp invocation reasons over. But getting the shape
right matters, because the shape *encodes the conflict-resolution policy* and the
*evidence discipline* that the rest of the design depends on.

## Derived fact — re-runnable, ground truth, no confidence

```lisp
;; DERIVED — re-runnable, ground truth, no confidence
(fact (calls refund-service.retry unit-of-work.commit)
  :source :callgraph :at "abc123f")
```

- The body is a plain relation: `(calls A B)`.
- `:source` names which substrate pillar produced it (`:callgraph`,
  `:dataflow`, `:dependency`, `:schema`, …).
- `:at` is the commit it was derived at.
- **No `:confidence`.** A derived fact does not get one. If it disagrees with a
  fresh parse, it was stale — re-derive it. (See [02](02-belief-types.md).)

## Descriptive belief — defeasible, defers to code

```lisp
;; INFERRED + DESCRIPTIVE — defeasible, defers to code
(belief billing-owns-refunds
  :kind :descriptive
  :claim (owns billing refunds)
  :confidence 0.94
  :justified-by ("billing/refunds.py@RefundService.refund@abc123") ; content-addressed
  :verified "2026-06-30"
  :on-conflict :demote)
```

Every field is load-bearing:

- `:kind :descriptive` — selects the conflict policy.
- `:claim` — the belief **stated in its own terms** (criterion 1 of
  [learned](04-learned-not-decorative.md)).
- `:confidence` — defeasible; rises on re-confirmation *in place* (no append).
- `:justified-by` — **content-addressed citation(s)** into the repo. This is what
  makes the belief re-derivable (criterion 2) **and** what drives invalidation: a
  diff touching `refunds.py` marks this belief potentially stale.
- `:verified` — when it was last checked against evidence.
- `:on-conflict :demote` — code wins; lower confidence / evict on conflict.

## Prescriptive invariant — challenges code, compiles to a check

```lisp
;; PRESCRIPTIVE — challenges code, compiles to a deterministic check
(invariant writes-through-uow
  :kind :prescriptive
  :claim (all-writes-dominated-by 'unit-of-work)
  :check (graph-query (forall w (writes-to-persistence)
                        (dominated-by w (frame 'unit-of-work))))
  :on-conflict :raise        ; NOT :mark-stale — it's a violation
  :enforcement :hard         ; inferred invariants get :soft
  :confidence :confirmed     ; human/compiler-confirmed, not LLM-asserted
  :counterexample nil)
```

- `:claim` — the rule in its own terms.
- `:check` — the **compiled deterministic query** (see
  [03](03-invariant-compiler.md)). This is the field that turns an opinion into
  something checkable. If an invariant can't be compiled, `:check` is absent and
  it stays soft.
- `:on-conflict :raise` — the inversion: a conflict is a **violation**, not a
  reason to weaken the rule.
- `:enforcement :hard` vs `:soft` — `:hard` may veto edits and is reserved for
  compiled-and-checked / human-confirmed invariants; inferred ones are `:soft`
  (warn only).
- `:confidence :confirmed` — note this is a *symbol*, not a float. Confirmed
  invariants aren't "0.9 likely"; they're confirmed. Inferred candidate
  invariants carry a numeric confidence and `:soft` enforcement.
- `:counterexample` — populated with a location when the check last failed; `nil`
  when passing.

## Rejected pattern / decision — a detector run against the *diff*

```lisp
;; REJECTED PATTERN — compiled detector run against the proposed DIFF
(decision reject-singleton-cache
  :status :rejected
  :detector (ast-match (singleton-construction-of 'cache))
  :accepted-pattern scoped-cache-provider
  :reason "singleton cache caused stale state during hot reload")
```

- `:status :rejected` — this is a decision *not* to do something.
- `:detector` — like an invariant's `:check`, but it runs against the **proposed
  diff**, so a rejected pattern is caught *as the agent tries to reintroduce it*,
  not after the fact.
- `:accepted-pattern` — points at what to do instead, so the veto is constructive.
- `:reason` — the durable intent. This is the part that survives even after
  `cache` is refactored away (archived, not deleted — see
  [05](05-memory-bounding.md)).

## Reading the field grammar at a glance

| Field | Appears on | Meaning |
|---|---|---|
| `:source` / `:at` | facts | which pillar, which commit |
| `:kind` | beliefs/invariants | selects conflict policy |
| `:claim` | beliefs/invariants | the statement in its own terms |
| `:confidence` | descriptive (float), invariants (`:confirmed` or float) | defeasibility |
| `:justified-by` | descriptive | content-addressed citation(s) |
| `:check` / `:detector` | invariants / decisions | the compiled deterministic query |
| `:on-conflict` | all beliefs | `:refresh` \| `:demote` \| `:raise` |
| `:enforcement` | invariants | `:hard` (may veto) \| `:soft` (warn) |
| `:counterexample` | invariants | location of last failure, or `nil` |
| `:accepted-pattern` / `:reason` | decisions | the constructive alternative + durable intent |

## One-line summary

The s-expression form makes the policy explicit on every belief: facts cite a
commit and carry no confidence; descriptive beliefs cite repo slices and `:demote`;
invariants compile to a `:check`, `:raise` on conflict, and gate enforcement on
confidence.
