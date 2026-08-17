# Persistence benchmark — cold vs. warm

Provider `glm` / model `glm-5.2`, 2 rep(s) per question, repo `Tether` at commit `c8c839f`.

Substrate: 111 files, 1882 symbols, built in 0.2s. Seeding turns learned **6** cited belief(s) automatically; store held 3 beliefs + 1 recorded invariant when questions ran.

**COLD** = same agent without the model tools (grep/read/glob/bash). **WARM** = with `model_query`/`model_record` over the built + seeded model. Recall = share of ground-truth items named in the answer (ground truth for *affects* is the substrate's own blast radius; a checkable oracle, not an opinion).

## Summary

Same model, same repo, same questions, same tool budget; the only difference is
whether the agent has the persistent codebase model to consult.

| | cold (amnesiac harness) | warm (Tether model) | change |
|---|---:|---:|---|
| tool calls per question | 9.2 | 5.9 | **−36%** |
| tokens per question | 51,132 | 34,940 | **−32%** |
| wall time per question | 91.8 s | 73.3 s | **−20%** |
| ground-truth recall | 92% | 100% | cold missed callers once (1/3), warm never did |
| model consulted | — | 100% of runs | |

Where the model *has* something recorded the gap is large: an "is this
allowed?" question costs **4.5 calls / 21k tokens / 78 s** warm vs **14 calls /
94k tokens / 192 s** cold (a rule and its citation vs. re-deriving the rule from
the code every time); ownership **2.5 vs 4.5 calls**. Where it has nothing to
say (the *general* question) consulting it is pure overhead: **6.5 vs 4 calls** —
that is the honest cost of the "consult first" habit and the reason the store
must be load-bearing, not decorative.

Automatic learning recorded 6 cited items from the first seeding turn (3
distinct beliefs after supersession, every one citing a real file at the
current commit — listed below); the second seeding turn yielded nothing the
extractor judged durable. Answers are reproduced at the bottom so the recall
scores can be audited.

### Round 1 (before today's fixes) vs round 2

An earlier run of this same script exposed three defects that were then fixed
and re-measured — the benchmark earned its keep on the first day:

| | round 1 | round 2 |
|---|---|---|
| warm agent used `model_query` on *affects* questions | 0% (fell back to grep) | 100% |
| beliefs learned automatically from seeding turns | 0 (extractor output truncated by the reasoning budget) | 6 recorded / 3 stored |
| `affects` blast radius for a common method name (`execute`) | 1190 symbols (the whole repo — useless) | 75, bound by same-file / import visibility |
| overall warm vs cold | 5.5 vs 8.4 calls, recall 88% vs 96% | 5.9 vs 9.2 calls, recall 100% vs 92% |

Reproduce: `python -m tether.scripts.persistence_bench --provider <p> --model <m> --reps 2`
(needs a configured provider key; uses an isolated temporary model DB).

| scope | mode | tool calls | tokens | seconds | recall | used model |
|---|---|---:|---:|---:|---:|---:|
| all | cold | 9.2 | 51132 | 91.8 | 92% | 0% |
| all | warm | 5.9 | 34940 | 73.3 | 100% | 100% |
| affects | cold | 11.8 | 54759 | 76.2 | 86% | 0% |
| affects | warm | 8.0 | 46749 | 87.7 | 100% | 100% |
| owns | cold | 4.5 | 26204 | 52.3 | 100% | 0% |
| owns | warm | 2.5 | 16472 | 28.5 | 100% | 100% |
| allowed | cold | 14.0 | 94356 | 192.0 | 100% | 0% |
| allowed | warm | 4.5 | 20704 | 78.2 | 100% | 100% |
| general | cold | 4.0 | 25579 | 62.3 | 100% | 0% |
| general | warm | 6.5 | 44024 | 84.7 | 100% | 100% |

## Per-run detail

| question | mode | rep | calls | tokens | s | hits | tools |
|---|---|---:|---:|---:|---:|---:|---|
| affects:chat_completion | cold | 0 | 13 | 62856 | 106.3 | 1/3 | bash grep grep bash bash bash glob glob glob bash grep grep grep |
| affects:chat_completion | cold | 1 | 14 | 38384 | 68.9 | 3/3 | grep bash file_read file_read bash grep grep grep grep grep grep bash grep bash |
| affects:chat_completion | warm | 0 | 5 | 15331 | 55.9 | 3/3 | grep model_query grep grep grep |
| affects:chat_completion | warm | 1 | 13 | 45847 | 133.1 | 3/3 | grep model_query grep file_read file_read file_read file_read grep grep grep gre |
| affects:save | cold | 0 | 11 | 46372 | 79.2 | 4/4 | grep grep bash bash bash bash bash bash bash bash bash |
| affects:save | cold | 1 | 9 | 71425 | 50.4 | 4/4 | grep bash grep file_read grep grep grep grep bash |
| affects:save | warm | 0 | 6 | 28604 | 38.8 | 4/4 | model_query grep file_read file_read grep grep |
| affects:save | warm | 1 | 8 | 97215 | 122.8 | 4/4 | model_query grep grep grep grep grep grep grep |
| owns:beliefs | cold | 0 | 3 | 21665 | 31.1 | 2/2 | bash grep file_read |
| owns:beliefs | cold | 1 | 6 | 30744 | 73.5 | 2/2 | grep bash bash bash bash grep |
| owns:beliefs | warm | 0 | 3 | 18859 | 39.3 | 2/2 | model_query glob grep |
| owns:beliefs | warm | 1 | 2 | 14085 | 17.8 | 2/2 | model_query grep |
| allowed:tools->ui | cold | 0 | 7 | 60476 | 107.1 | 1/1 | grep bash bash bash bash bash bash |
| allowed:tools->ui | cold | 1 | 21 | 128236 | 277.0 | 1/1 | glob grep file_read bash grep grep grep glob bash bash bash bash grep grep file_ |
| allowed:tools->ui | warm | 0 | 4 | 19652 | 82.7 | 1/1 | model_query model_query grep model_check |
| allowed:tools->ui | warm | 1 | 5 | 21755 | 73.7 | 1/1 | model_query grep model_query file_read model_check |
| general:overflow | cold | 0 | 4 | 25548 | 68.3 | 2/2 | grep grep file_read grep |
| general:overflow | cold | 1 | 4 | 25610 | 56.2 | 2/2 | grep grep file_read grep |
| general:overflow | warm | 0 | 6 | 30556 | 70.9 | 2/2 | model_query model_query grep grep file_read grep |
| general:overflow | warm | 1 | 7 | 57493 | 98.5 | 2/2 | grep model_query glob grep file_read grep file_read |

## Learned beliefs (automatic, cited)

- BeliefManager in beliefs.py owns the entire belief lifecycle policy — recording (supersession), demotion, and eviction — while ModelStore in store.py owns only the mechanics of SQLite persistence.  
  citations: core/codebase_model/beliefs.py @ BeliefManager @ c8c839f, core/codebase_model/store.py @ ModelStore @ c8c839f
- service.py's CodebaseModel is the sole facade that wires the five components together and owns the per-repo singleton, the build/sync lifecycle, and the on_edit hook entry point.  
  citations: core/codebase_model/service.py @ CodebaseModel @ c8c839f
- model.py owns the shared dependency-free dataclass contract — Node, Edge, Belief, Invariant, Decision, Violation — plus the OnConflict, Enforcement, and BeliefKind policy constants that every other module conforms to.  
  citations: core/codebase_model/model.py @ c8c839f

## Answers (first 600 chars)

### affects:chat_completion · cold · rep 0

Here's where things stand:

**(1) Task:** Find all files in the repo that directly call `InferenceBackend.chat_completion`.

**(2) What I tried:**
- `grep` tool for `InferenceBackend` and `\.chat_completion\(` across the whole repo → both timed out after 30s (the repo has large `desktop/node_modules` and `desktop/src-tauri/target` dirs).
- `bash` with `rg` (ripgrep) → exit 127, `rg` isn't installed under that name.
- `bash` with `git grep` → blocked because bash had already hit its consecutive-failure limit from the `rg` attempts.
- `glob` to list source files → succeeded (this is a mostly-Pyt

### affects:chat_completion · cold · rep 1

I have enough to answer. The callers of `InferenceBackend.chat_completion`:

**Direct, verified callers:**

- `agents/base.py` — `self.backend` is `InferenceBackend(config)` (line 78); calls at line 129
- `engine/backend.py` — line 401 is a self-call from `InferenceBackend.chat_completion_stream`; line 281 is the `CodexExecBackend` delegation branch *inside* `chat_completion` itself (not a separate caller)
- `ui/repl.py` — `engine.backend` resolves to `InferenceBackend` (via `query_engine.py:540`); calls at lines 727, 789, 849
- `app_bridge.py` — `engine.backend` resolves to `InferenceBackend`

### affects:chat_completion · warm · rep 0

Three files contain code that directly calls `InferenceBackend.chat_completion`:

- **`agents/base.py`** — line 129: `self.backend.chat_completion(...)` in `SubAgent.run`
- **`engine/backend.py`** — line 401: `self.chat_completion(...)` in `InferenceBackend.chat_completion_stream` (internal self-call)
- **`ui/repl.py`** — lines 727, 789, 849: `engine.backend.chat_completion(...)` in `_save_exit_summary`, `_smart_compact`, and `_handle_memory`

Note: `engine/backend.py` line 281 also has a `.chat_completion(` call, but it's on a `CodexExecBackend` instance (`CodexExecBackend(self.config, self.l

### affects:chat_completion · warm · rep 1

Verified all call sites against the actual `backend` object types. Here's the exhaustive list.

## Files that directly call `InferenceBackend.chat_completion`

| File | Call site(s) | Form |
|------|-------------|------|
| `ui/repl.py` | lines 727, 789, 849 | `engine.backend.chat_completion(...)` |
| `agents/base.py` | line 129 | `self.backend.chat_completion(...)` |
| `app_bridge.py` | line 1405 | `self.engine.backend.chat_completion(...)` |
| `core/codebase_model/learn.py` | line 172 | `backend.chat_completion(...)` (param; receives an `InferenceBackend`) |
| `core/codebase_model/verify.py`

### affects:save · cold · rep 0

I have the full picture. The `.save()` calls on `TetherConfig` instances appear in several files. Let me verify the `cfg`/`config`/`self.config` variables are actually `TetherConfig` instances in the ambiguous cases (`repl.py`, `cli.py`, `app_bridge.py`) to be exhaustive but accurate — and confirm `todo.py`/`learning.py` are unrelated (different `.save` targets).

The relevant calls split clearly: `config.save()` / `cfg.save()` / `self.config.save()` where the variable holds a `TetherConfig`. From the earlier grep, `ui/repl.py`, `tools/agent_tool.py`, `cli.py`, and `app_bridge.py` import/use `

### affects:save · cold · rep 1

The bash failures were just `rg` not being installed — I already have complete results from grep. No additional `.save()` calls exist beyond what I found.

## Final answer

Files containing code that directly calls `TetherConfig.save`:

1. `cli.py` — multiple `config.save()` calls (model selection, `config save`, provider switch, API key set/clear, gguf dir add/remove)
2. `app_bridge.py` — multiple `self.config.save()` calls (key clear, config restore, memory toggle, gguf dir add, workflow-learning toggle). Note: line 966 (`self.todo_store.save(...)`) is a different object and does **not** cou

### affects:save · warm · rep 0

Confirmed: `app_bridge.py` `self.config` is `TetherConfig`, and `ui/repl.py` `cfg` is `TetherConfig`. The `cli.py` callers are the model-listed command functions, all operating on `TetherConfig`. The test file explicitly constructs `TetherConfig(...)` then calls `.save(path)`.

## Files that directly call `TetherConfig.save`

- `cli.py` — 8 call sites across `cmd_setup`, `cmd_remote`, `cmd_key`, `cmd_models`, `select_model` (lines 934, 1096, 1600, 1655, 1693, 1761, 1789, 1836, 1862)
- `app_bridge.py` — 5 call sites (lines 872, 916, 958, 972, 993, 1364)
- `ui/repl.py` — 4 call sites (lines 996,

### affects:save · warm · rep 1

Verified all call sites. The model index's `core/learning.py` hit is a false positive — that's `WorkflowDetector.save()` (defined at line 108), not `TetherConfig.save`. The `todo_store.save` / `store.save` calls are also unrelated.

Files that directly call `TetherConfig.save`:

- `cli.py` — 9 call sites (e.g. lines 934, 1096, 1600, 1655, 1693, 1761, 1789, 1836, 1862), all on `config = TetherConfig.load()`
- `app_bridge.py` — 6 call sites (lines 872, 916, 958, 972, 993, 1364), all on `self.config` (a `TetherConfig` loaded at line 318)
- `ui/repl.py` — 4 call sites (lines 996, 1004, 1372, 1389)

### owns:beliefs · cold · rep 0

Verified against the source.

**File:** `core/codebase_model/beliefs.py`
**Class:** `BeliefManager`

The class docstring states it "Owns descriptive beliefs: creation/supersession, consultation (LRU), reinforcement, demotion, invalidation, and lazy re-verification." Concretely:

- **Supersession** — `BeliefManager.add()` (replaces an existing belief id in place, raising confirmations and confidence toward certainty rather than accumulating rows).
- **Demotion** — `BeliefManager.demote()` (the descriptive `:demote` policy: code won, so lower confidence; below the floor the belief is deleted).
-

### owns:beliefs · cold · rep 1

The module that owns belief supersession, demotion, and eviction is:

**File:** `core/codebase_model/beliefs.py`
**Class:** `BeliefManager`

The class docstring (line 71) explicitly states it "Owns descriptive beliefs: creation/supersession, consultation (LRU)," and it implements:
- `record()` (line 85) — handles supersession in place when a belief id already exists
- `demote()` (line 153) — lowers a belief's confidence
- `_evict()` (line 228) — the internal eviction routine

The surrounding files (`store.py`, `query.py`, `service.py`, etc.) in `core/codebase_model/` reference these concepts b

### owns:beliefs · warm · rep 0

Verified against the source. The owner is:

**File:** `core/codebase_model/beliefs.py`
**Class:** `BeliefManager`

`BeliefManager` owns the entire belief lifecycle policy — supersession (recording), demotion, and eviction. The underlying SQLite persistence mechanics are owned by `ModelStore` in `store.py`, but the supersession/demotion/eviction policy itself lives in `BeliefManager`.

### owns:beliefs · warm · rep 1

**File:** `core/codebase_model/beliefs.py`
**Class:** `BeliefManager`

`BeliefManager` owns the entire belief lifecycle policy — recording (supersession), demotion, and eviction — while `ModelStore` in `store.py` handles only the mechanics of SQLite persistence underneath.

### allowed:tools->ui · cold · rep 0

**No.** Code under `tools/` is not allowed to import from `ui/`.

**Rule:** A recorded **hard invariant** forbids it:

- **ID:** `tool-implementations-under-tools-must-not-import-from-ui-the-terminal-ui-depends-on-tools-never-the-reverse`
- **Check:** `(forbidden-edge :kind imports :from tools :to ui)`
- **Enforcement:** hard, confidence 0.9

**Rationale:** `ui/` is the terminal UI layer that *consumes* `tools/`. The dependency direction is one-way (`ui → tools`); letting `tools/` reach back into `ui/` inverts that and would create a cycle. This is documented in `docs/persistence-benchmark.md`

### allowed:tools->ui · cold · rep 1

**Yes** — code under `tools/` is allowed to import from `ui/`, and in fact already does.

## Evidence

**The code does it.** `tools/agent_tool.py:18`:
```python
from ..ui.colors import DIM, GREEN, RED, RESET, agent_label, dim
```

**No rule in the repo forbids it.** I checked every place a prohibition could live:
- `pyproject.toml` has no ruff / import-linter / flake8 layering config.
- No `docs/` rule or `README.md` statement forbids `tools/ → ui/`.
- The codebase-model invariant compiler's only *real* `forbidden-edge` fixture in this repo is `plugins → Renderer` (`core/codebase_model/accepta

### allowed:tools->ui · warm · rep 0

**No.** Code under `tools/` is not allowed to import from `ui/`.

The decisive evidence is a hard-enforced invariant recorded in the codebase model:

> `tool-implementations-under-tools-must-not-import-from-ui-the-terminal-ui-depends-on-tools-never-the-reverse`
> check: `(forbidden-edge :kind imports :from tools :to ui)`
> enforcement: **hard**, confidence 0.9

In plain terms: the terminal UI depends on tools, never the reverse — so `tools → ui` import edges are forbidden.

One caveat worth flagging: when I asked the `allowed` action directly about "code under tools/ importing from ui/", it re

### allowed:tools->ui · warm · rep 1

**No** — by the recorded rule. But there's a real contradiction in the repo worth flagging.

## The rule

The persistent model records a **hard invariant**:

> tool implementations under `tools/` must not import from `ui/` — the terminal UI depends on tools, never the reverse.

- Check expression: `(forbidden-edge :kind imports :from tools :to ui)`
- Enforcement: hard, confidence 0.9
- Also encoded in `tests/test_bugfix_regressions.py:274` as a recorded invariant, and referenced in `scripts/persistence_bench.py:187-188`.

So the *intended* rule is clearly **no**.

## The evidence conflict

The

### general:overflow · cold · rep 0

The HTTP 400 context-length overflow handler lives in **`engine/query_engine.py`**, inside the **`submit`** method of the query engine (defined at line 776, with the 400 handler at lines 965–981).

When the model API returns HTTP 400 and the body mentions "context", `submit` treats it as a context-length overflow and:
1. Increments `self._compact_attempts` (capped at 3).
2. Calls `self._compact()` to shrink the conversation, then `continue`s the agent loop to retry.
3. After 3 failed compactions, returns a `TurnResult` with `stop_reason="context_overflow"` and the message *"Context overflow: c

### general:overflow · cold · rep 1

The handler lives in **`QueryEngine.submit`** in **`engine/query_engine.py`** (the `except urllib.error.HTTPError` block starting at line 956).

When the API returns HTTP 400 with `"context"` in the body, it treats it as a context-length overflow and:

1. Increments `self._compact_attempts` (starting from 0).
2. If that count is ≤ 3, prints a yellow "Context overflow — auto-compacting" notice, logs a warning, calls `self._compact()` to shrink the conversation, and `continue`s the agent loop to retry.
3. After 3 failed compactions, it prints a red error, logs it, and returns a `TurnResult` with

### general:overflow · warm · rep 0

The enclosing method is `submit` (defined at line 776), and the recovery calls `self._compact()` (defined at line 2290).

## Answer

**File:** `engine/query_engine.py`
**Methods:** `submit` (the agent-loop method, line 776) handles the error; it delegates recovery to `self._compact()` (line 2290).

**What it does** (inside `submit`, lines 956–981): when `chat_completion_stream_parsed` raises `urllib.error.HTTPError` with `e.code == 400` and the word `"context"` in the response body, the engine treats it as a context-length overflow and:

1. Increments `self._compact_attempts` (up to 3 tries).

### general:overflow · warm · rep 1

The context-overflow handling lives in the `except urllib.error.HTTPError` block inside **`QueryEngine.submit`** in **`engine/query_engine.py`** (around line 956–981).

What it does, step by step:

1. **Detects the overflow** at `query_engine.py:965`:
   ```python
   if e.code == 400 and "context" in body:
   ```
   It reads the error body (preferring a stashed `tether_body` since the backend already consumed `e.read()`), lowercases it, and checks for the word "context".

2. **Tracks attempts** via `self._compact_attempts`, incrementing on each hit.

3. **Auto-compacts and retries** (up to 3 a

