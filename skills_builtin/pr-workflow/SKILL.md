---
name: PR Workflow
description: Branch, commit cleanly, and open a pull request the right way.
version: 1.0.0
category: git
platforms: [macos, linux]
requires_toolsets: [bash]
---

## When to Use
You're about to commit work, or the user asks to "open a PR", "push this", or
"make a branch". Use it to avoid committing to the wrong branch or bundling
unrelated changes.

## Procedure
1. **Check state first.** `git status` and `git diff` — know exactly what will be
   committed before you stage anything.
2. **Branch if needed.** Never commit straight to `main`/`master`. If you're on
   the default branch, create a topic branch: `git checkout -b <type>/<short-desc>`.
3. **Stage deliberately.** Add only the files that belong to this change. Don't
   `git add -A` over unrelated edits or generated files.
4. **Commit with a clear message.** A concise subject line in the imperative
   ("Add retry to upload"), then a body explaining *why* if it isn't obvious.
5. **Push and open the PR.** `git push -u origin <branch>`, then open the PR with
   the platform CLI (e.g. `gh pr create`) when available. Fill in a short summary
   and a test plan.

## Pitfalls
- Committing on `main` because you forgot to branch.
- Sweeping unrelated files into the commit with a broad `git add`.
- Force-pushing or rewriting shared history without being asked.
- Committing secrets or large build artifacts — check the diff.

## Verification
`git log --oneline -1` shows your commit on the topic branch, `git status` is
clean, and the PR link is returned to the user.
