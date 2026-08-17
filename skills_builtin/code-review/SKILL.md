---
name: Code Review
description: Review a diff or PR for real bugs first, style last — with evidence.
version: 1.0.0
category: quality
when_to_use: The user asks to review changes, a branch, a PR, or "check my code".
argument_hint: "[file, branch, or PR — defaults to the working diff]"
---

## When to Use
The user asks for a review of uncommitted changes, a branch, or a pull
request — or before committing a large piece of your own work.

## Procedure
1. **Get the actual diff.** `git diff` (working tree), `git diff main...HEAD`
   (branch), or the named files. Review what changed, not what you remember
   changing.
2. **Understand intent first.** Read the commit messages / user's description.
   A review that doesn't know the goal flags the wrong things.
3. **Pass 1 — correctness.** For each hunk ask: what input or state makes this
   wrong? Look for inverted conditions, off-by-one, unhandled None/empty/error
   paths, resource leaks, and behavior changes callers don't expect. Read the
   *surrounding* code when a hunk's context is unclear — bugs live at the seams.
4. **Pass 2 — blast radius.** Who calls the changed code? Grep for callers of
   changed functions and check their assumptions still hold.
5. **Pass 3 — tests and style.** Are the risky paths tested? Only then note
   style issues, and only ones that hurt readability — don't nitpick taste.
6. **Report ranked findings.** Most severe first. For each: file:line, what
   breaks, the concrete input/state that triggers it, and a suggested fix.
   Say "no correctness issues found" explicitly if that's the outcome.

## Pitfalls
- Reviewing the description instead of the diff.
- Flagging style while missing the null-deref two lines up.
- Claiming a bug without a concrete failing scenario — verify before reporting.
- Rubber-stamping: every review should show evidence you read the seams.

## Verification
Each reported finding names a file:line and a concrete trigger; each was
checked against the surrounding code, not just the hunk.
