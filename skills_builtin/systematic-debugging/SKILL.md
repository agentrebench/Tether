---
name: Systematic Debugging
description: Reproduce, isolate, fix, and verify a bug without guessing.
version: 1.0.0
category: debugging
---

## When to Use
A test is failing, behaviour is wrong, or an error keeps recurring and the
cause isn't obvious. Reach for this before changing code speculatively.

## Procedure
1. **Reproduce.** Find the smallest command or input that triggers the bug.
   Capture the exact error text and a stack trace. If you can't reproduce it,
   stop and gather more signal — don't fix blind.
2. **Locate.** Use `grep`/`glob` to find the code on the failing path. Read the
   relevant functions in full before forming a hypothesis.
3. **Hypothesise one cause.** State it in a sentence: "X fails because Y." If you
   have several, rank them and test the cheapest to confirm first.
4. **Confirm before fixing.** Add a print/log or a focused check that proves the
   hypothesis, or read the code path closely enough to be certain. Don't fix a
   cause you haven't confirmed.
5. **Fix narrowly.** Change the smallest thing that addresses the confirmed
   cause. Avoid drive-by refactors in the same edit.
6. **Verify.** Re-run the original reproduction. Then run the surrounding tests
   to check you didn't break a neighbour.

## Pitfalls
- Changing several things at once so you can't tell what fixed it.
- "Fixing" a symptom (swallowing an exception) instead of the cause.
- Trusting that the fix worked without re-running the exact failing case.

## Verification
The original reproduction now passes, related tests still pass, and you can
explain in one sentence why the change fixes the confirmed cause.
