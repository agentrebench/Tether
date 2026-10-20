# Runtime migration: baseline and v1 contracts

This increment preserves the Python execution loop. It does not add a native
runtime, durable scheduling, automatic worktree creation, or crash resumption.
Those remain the subsequent phases in `AGENTS.md`.

## Task plan wire format

`runtime/contracts.py` defines UTF-8 JSON messages, version **1**, bounded to
1 MiB and 256 tasks per plan. The envelope contains exactly `version`, `type`
(`plan`), `run_id`, and `tasks`. Unknown versions/fields, duplicate JSON keys,
duplicate IDs, foreign parent runs, absent dependencies, and cycles are rejected.

Each task carries an ID, run ID, objective, full Git base object ID, explicit
token/time/cost/attempt budget, acceptance criteria, expected artifact paths,
dependency IDs, impacted files and symbols, worker role, and verification argv
arrays. Paths are relative to the workspace; the receiving execution layer
must still resolve symlinks and enforce OS permissions before accessing them.
Budgets must be finite. Zero cost allows local execution; it is not unlimited.
These structures validate requested budgets; they do not enforce resource use.

The state contract follows `CREATED → SCOPED → PLANNED → RUNNING → VERIFYING →
COMPLETED`, with blocked/failed/cancelled/paused exits. Completion requires an
explicit verified-evidence gate. Repair from VERIFYING to RUNNING requires a
remaining retry. Terminal states cannot resume. A future journal must persist
the previous phase for PAUSED/BLOCKED, along with attempts, timestamps, worker
and worktree assignment, patch hashes, and structured verification evidence;
resumption must reconcile the actual checkout before advancing that phase.

Future commands must carry unique request IDs. Identical duplicate requests
return the recorded result without running again; an ID reused with different
content is a protocol error. Events have increasing per-run sequence numbers.
Before publishing durable events, commit state and event in one transaction.
On restart reject sequence gaps or inconsistent state rather than guessing from
a transcript. The diagnostic traces below are not that durable journal.

## Optional Python trace

Set `runtime_trace_dir` in `config.json` to an absolute local directory. Empty
(default) performs no trace I/O. `QueryEngine.submit` creates one mode-0600 JSONL
file per submission in a mode-0700 directory when the directory is new. Existing
directories retain their permissions. Events are:

| Event | Data |
| --- | --- |
| `run.started` | protocol version, run ID, sequence, elapsed nanoseconds, provider/model labels |
| `model.finished` | elapsed call time, return/raise/cancel status, reported input/output tokens |
| `tool.finished` | elapsed execution/render time, tool name, error flag, output byte count |
| `run.finished` | return/raise status, engine stop reason, per-submission token delta, dropped-event count |

Elapsed times use a monotonic clock. Tool execution events are serialized across
the engine's worker threads. The final event has a reserved slot in the 10,000
event limit. New traces stop when the directory contains 100 matching trace
files; archive them to record more. No files are automatically deleted. This
directory cap is best effort across independent processes. No prompt, response,
arguments, file contents, raw exception text, or credentials are recorded.
Trace failures warn and preserve the engine's return/exception behavior.

`returned` means the Python call returned, not that an engineering objective was
verified. A killed process may leave an incomplete JSONL trace. Trace results
do not authorize resumption or completion. Model timing includes backend retry
time; tool timing includes status rendering. Subagent-internal model calls,
Codex's separate backend, provider cache hits/costs, subprocess CPU/RAM, and
indexing metrics are not yet covered by this host-engine trace.

## Reproducible engineering fixture harness

Run `python scripts/runtime_bench.py --output <new-directory> --repetitions 5`.
This uses the real query engine, read/edit tools, workspace policy, Git patches,
and Python acceptance subprocesses. Only the model responses are replay fixtures.
Three cases cover a focused edit, a boundary-condition bug fix, and dependent
changes in two files. Each acceptance check must fail on the initial fixture
and pass on the final tree. The checker lives outside the editable workspace.
The recorded patch must reverse-apply cleanly to the final tree.

Each case is repeated with tracing off/on in alternating order. `report.json`
contains source hashes, fixture commits, interpreter/platform metadata, all
individual runs, p50/nearest-rank p95, patch hashes, exact verification argv,
return codes, and output paths. Tokens are explicitly simulated and there are
no API charges. Fixture repositories and evidence are retained under the output
directory; the output directory must not already exist.

This establishes correctness and instrumentation overhead for replayed tasks.
It is not evidence of autonomous model quality or native speedup. Remaining
Phase 0 work includes budgeted live-provider benchmark adapters, cost/cache and
subagent instrumentation, and repeated same-model comparisons on pinned real
repositories. Phase 1 adds the C23 package, SQLite journal, and recovery tests;
later phases add scheduling, isolated workers, and verified integration.
