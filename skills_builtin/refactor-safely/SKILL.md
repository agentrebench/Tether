---
name: Refactor Safely
description: Restructure code in small verified steps without changing behavior.
version: 1.0.0
category: quality
when_to_use: The user asks to refactor, clean up, restructure, or "make this nicer" without changing what it does.
argument_hint: "[file or area to refactor]"
---

## When to Use
The user asks to refactor, extract, rename, deduplicate, or simplify existing
code — anything where behavior must stay identical while structure improves.

## Procedure
1. **Establish the safety net first.** Run the existing tests for the area and
   record what passes. If the code has no tests and the refactor is risky,
   write a couple of pin-down tests for current behavior *before* touching it.
2. **Map the seams.** Grep every caller / importer of what you're about to
   change. The refactor's real scope is the callers, not the file.
3. **One transformation per step.** Rename, then move, then extract — never
   all three in one edit. After each step the code compiles and tests pass.
   Prefer several small `file_edit` batches over one sweeping rewrite.
4. **Keep behavior frozen.** No "while I'm here" fixes: if you find a real bug
   mid-refactor, note it and report it separately — fixing it silently inside
   a refactor hides both changes from review.
5. **Update every caller in the same step** as the signature/name change, and
   grep again afterward for stragglers (strings, docs, configs reference names
   too).
6. **Final pass.** Run the full test suite, then diff-read your own change as
   a reviewer would: is every hunk explainable by the stated refactor?

## Pitfalls
- Refactoring and bug-fixing in the same change — reviewers can't tell which
  hunks are behavior-neutral.
- Renaming a symbol but missing string references (CLI names, config keys,
  log messages, docs).
- Trusting "it still compiles" — behavior lives in tests, not the type checker.
- A "small cleanup" that snowballs; if scope grows, stop and report.

## Verification
Full test suite is green, every caller of changed symbols was updated, and
the diff contains no hunk you can't attribute to the stated transformation.
