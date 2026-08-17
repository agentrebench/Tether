---
name: Write Tests
description: Add tests that pin behavior — failure cases first, happy path second.
version: 1.0.0
category: testing
when_to_use: The user asks for tests, or you changed logic that has none.
argument_hint: "[file or function to cover]"
---

## When to Use
The user asks for tests, you just wrote or changed non-trivial logic, or a bug
fix needs a regression test so it can't come back.

## Procedure
1. **Find the house style.** Read one or two existing test files: runner
   (pytest/unittest), naming, fixtures, directory layout, how they import the
   code under test. Match it exactly — don't introduce a new pattern.
2. **List behaviors, not functions.** For the code under test write down: the
   contract's happy path, each documented edge (empty, None, zero, huge,
   duplicate), and each error path. A bug fix gets a test that fails on the
   old code first.
3. **Write failure cases first.** Error paths and edges catch more regressions
   than the happy path, and they force you to learn the real contract.
4. **One behavior per test.** Name it after the behavior
   (`test_timeout_kills_process`), not the method. Assert on outcomes, not
   internals — a test coupled to private state breaks on every refactor.
5. **Make them hermetic.** Temp dirs over real paths, fakes over network,
   explicit clocks over sleeps. A test that can flake will flake.
6. **Run them, then run the whole suite.** New tests must pass, and must not
   have broken the neighbors. If a new test passes without your change, it's
   not testing the change — fix the test.

## Pitfalls
- Testing the mock instead of the code (assert the outcome, not that a mock
  was called, unless the call *is* the contract).
- Copying the implementation's logic into the assertion — the test then
  proves nothing.
- Snapshot/golden tests for logic that has a checkable property.
- Skipping the "fails on old code" check for regression tests.

## Verification
New tests fail when the behavior is broken (try reverting the change
mentally), pass with it, and the full suite is green.
