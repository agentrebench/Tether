#!/usr/bin/env python3
"""Cold vs. warm: does the persistent codebase model earn its keep?

Runs the same understanding questions against this repository through two
otherwise-identical agents using a real provider:

  COLD  fresh engine, no codebase-model tools (grep/read/glob/bash only) —
        the ordinary "amnesiac harness" baseline.
  WARM  fresh engine + model_query/model_record, against a model that was
        built (substrate) and had a chance to *learn* from a couple of prior
        exploration turns — i.e. what a returning Tether session has.

For every (question, mode, repetition) it records tool calls, tokens, wall
time, and whether the answer names the ground-truth files/symbols. Ground
truth for "affects" questions is the substrate's own blast radius (checkable),
for "owns/allowed" the seeded facts. It also reports how many cited beliefs
the WARM agent learned automatically during the seeding turns.

Usage:
  python -m tether.scripts.persistence_bench --provider glm --model glm-5.2 --reps 2
  (any built-in provider with a configured key; writes docs/persistence-benchmark.md)
"""
from __future__ import annotations

import argparse
import contextlib
import copy
import io
import json
import statistics
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

from tether.core.config import TetherConfig, apply_provider_selection  # noqa: E402
from tether.core.codebase_model import service as cbm_service  # noqa: E402
from tether.core.codebase_model.service import CodebaseModel  # noqa: E402
from tether.core.codebase_model.learn import learn_from_turn  # noqa: E402
from tether.core.permissions import PermissionContext  # noqa: E402
from tether.engine.query_engine import QueryEngine  # noqa: E402
from tether.tools.base import ToolRegistry  # noqa: E402


@dataclass
class Question:
    key: str
    kind: str          # affects | owns | allowed | general
    prompt: str
    truth: list[str]   # substrings the answer must contain to count as correct


@dataclass
class Run:
    question: str
    mode: str
    rep: int
    tool_calls: int
    tokens: int
    seconds: float
    hits: int
    truth_n: int
    used_model: bool
    answer: str = ""
    tools: list[str] = field(default_factory=list)


def make_config(provider: str, model: str) -> TetherConfig:
    config = TetherConfig.load()
    config = copy.deepcopy(config)
    apply_provider_selection(config, provider, model)
    # Read-only agent: writes denied, everything else auto-approved so the run
    # is unattended and both modes see the same tool surface.
    config.auto_approve_reads = True
    config.auto_approve_bash = True
    config.auto_approve_edits = True
    config.deny_tools = ["file_write", "file_edit", "agent", "web_fetch", "todo_write"]
    config.max_turns = 14
    config.codebase_model_enabled = True
    return config


def build_engine(config: TetherConfig, *, warm: bool) -> QueryEngine:
    registry = ToolRegistry.build_default(
        include_codebase_model=warm, workspace_root=REPO, enforce_workspace=False,
    )
    for name in list(config.deny_tools):
        registry.tools.pop(name, None)
    return QueryEngine(
        config=config,
        tool_registry=registry,
        permissions=PermissionContext.from_config(config),
        on_approval_request=lambda *_a, **_k: "deny",
        persistent_context_enabled=False,
        include_last_session_summary=False,
    )


def run_turn(engine: QueryEngine, prompt: str) -> tuple[str, int, int, float, list[str]]:
    before_in, before_out = engine.usage.input_tokens, engine.usage.output_tokens
    t0 = time.monotonic()
    sink = io.StringIO()
    with contextlib.redirect_stdout(sink):
        result = engine.submit(prompt)
    seconds = time.monotonic() - t0
    tokens = (engine.usage.input_tokens - before_in) + (engine.usage.output_tokens - before_out)
    return result.output or "", len(result.tool_calls_made), tokens, seconds, list(result.tool_calls_made)


def score(answer: str, truth: list[str]) -> int:
    lowered = answer.lower()
    return sum(1 for t in truth if t.lower() in lowered)


def ground_truth_questions(model: CodebaseModel) -> list[Question]:
    """Questions whose answers the substrate can *check*."""
    qs: list[Question] = []
    for symbol, label in [
        ("engine/backend.py::InferenceBackend.chat_completion", "InferenceBackend.chat_completion"),
        ("core/config.py::TetherConfig.save", "TetherConfig.save"),
    ]:
        callers = model.indexer.blast_radius(symbol, max_depth=1)
        files = sorted({c.split("::", 1)[0] for c in callers})
        # keep the question answerable in prose: name the files
        qs.append(Question(
            key=f"affects:{label.split('.')[-1]}", kind="affects",
            prompt=(f"In this repository, which files contain code that directly calls "
                    f"`{label}`? List the file paths. Be exhaustive but list only files "
                    f"that actually call it."),
            truth=files,
        ))
    qs.append(Question(
        key="owns:beliefs", kind="owns",
        prompt=("Which module in this repository owns belief supersession, demotion and "
                "eviction for the persistent codebase model? Name the file and the class."),
        truth=["core/codebase_model/beliefs.py", "BeliefManager"],
    ))
    qs.append(Question(
        key="allowed:tools->ui", kind="allowed",
        prompt=("Is code under tools/ allowed to import from ui/ in this repository? "
                "Answer yes or no, and say what rule or evidence you based it on."),
        truth=["no"],
    ))
    qs.append(Question(
        key="general:overflow", kind="general",
        prompt=("What does the query engine do when the model API returns HTTP 400 for "
                "a context-length overflow? Name the method and the file."),
        truth=["_compact", "engine/query_engine.py"],
    ))
    return qs


SEED_TASKS = [
    "Explain how the persistent codebase model records, demotes and evicts beliefs. "
    "Read the relevant code under core/codebase_model and summarize who owns what.",
    "Explain how tools are registered and dispatched, and which modules import ui/. "
    "Read tools/base.py, engine/query_engine.py and any files you need.",
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", default="glm")
    ap.add_argument("--model", default="")
    ap.add_argument("--reps", type=int, default=2)
    ap.add_argument("--out", default=str(REPO / "docs" / "persistence-benchmark.md"))
    ap.add_argument("--no-seed", action="store_true", help="skip the learning turns")
    args = ap.parse_args()

    config = make_config(args.provider, args.model)
    print(f"provider={config.provider} model={config.api_model}")

    # Isolated model store for this run (never the user's).
    db = Path(tempfile.mkdtemp(prefix="tether-bench-")) / "model.db"
    model = CodebaseModel(REPO, db_path=db)
    cbm_service._MODELS[str(cbm_service._resolve_root(REPO))] = model
    t0 = time.monotonic()
    rep = model.build()
    build_s = time.monotonic() - t0
    print(f"substrate: {rep.get('indexed')} files, {model.store.count_nodes()} symbols in {build_s:.1f}s")

    # A rule the WARM agent can consult (COLD has to infer it from code).
    model.record_invariant(
        "Tool implementations under tools/ must not import from ui/ (the terminal UI depends on tools, never the reverse).",
        check="(forbidden-edge :kind imports :from tools :to ui)",
        enforcement="hard", confidence=0.9, source="human",
    )

    learned = 0
    if not args.no_seed:
        seed_engine = build_engine(config, warm=True)
        for task in SEED_TASKS:
            print(f"seed: {task[:60]}…")
            run_turn(seed_engine, task)
            report = learn_from_turn(model, seed_engine.backend, seed_engine.last_turn_messages())
            learned += len(report.get("recorded", []))
            print(f"  learned {len(report.get('recorded', []))} cited belief(s) ({report.get('reason') or 'ok'})")
    beliefs = model.beliefs.all()
    print(f"beliefs in store: {len(beliefs)}")

    questions = ground_truth_questions(model)
    runs: list[Run] = []
    for q in questions:
        for mode in ("cold", "warm"):
            for r in range(args.reps):
                engine = build_engine(config, warm=(mode == "warm"))
                answer, calls, tokens, seconds, tools = run_turn(engine, q.prompt)
                hits = score(answer, q.truth)
                used_model = any(t.startswith("model_") for t in tools)
                runs.append(Run(q.key, mode, r, calls, tokens, seconds, hits, len(q.truth), used_model, answer[:600], tools))
                print(f"{q.key:22} {mode:4} rep{r}: calls={calls:2} tokens={tokens:6} {seconds:5.1f}s hits={hits}/{len(q.truth)} model={'y' if used_model else 'n'}")

    # ---- report ---------------------------------------------------------
    def agg(mode: str, kind: str | None = None):
        rows = [x for x in runs if x.mode == mode and (kind is None or x.question.startswith(kind))]
        if not rows:
            return None
        return {
            "n": len(rows),
            "calls": statistics.mean(x.tool_calls for x in rows),
            "tokens": statistics.mean(x.tokens for x in rows),
            "seconds": statistics.mean(x.seconds for x in rows),
            "recall": sum(x.hits for x in rows) / max(1, sum(x.truth_n for x in rows)),
            "model_used": sum(1 for x in rows if x.used_model) / len(rows),
        }

    lines = [
        "# Persistence benchmark — cold vs. warm",
        "",
        f"Provider `{config.provider}` / model `{config.api_model}`, {args.reps} rep(s) per question, "
        f"repo `{REPO.name}` at commit `{model.indexer.current_commit()}`.",
        "",
        f"Substrate: {rep.get('indexed')} files, {model.store.count_nodes()} symbols, built in {build_s:.1f}s. "
        f"Seeding turns learned **{learned}** cited belief(s) automatically; store held {len(beliefs)} beliefs "
        f"+ 1 recorded invariant when questions ran.",
        "",
        "**COLD** = same agent without the model tools (grep/read/glob/bash). "
        "**WARM** = with `model_query`/`model_record` over the built + seeded model. "
        "Recall = share of ground-truth items named in the answer (ground truth for *affects* "
        "is the substrate's own blast radius; a checkable oracle, not an opinion).",
        "",
        "| scope | mode | tool calls | tokens | seconds | recall | used model |",
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    for kind, label in [(None, "all"), ("affects", "affects"), ("owns", "owns"), ("allowed", "allowed"), ("general", "general")]:
        for mode in ("cold", "warm"):
            a = agg(mode, kind)
            if a:
                lines.append(f"| {label} | {mode} | {a['calls']:.1f} | {a['tokens']:.0f} | {a['seconds']:.1f} | {a['recall']:.0%} | {a['model_used']:.0%} |")
    lines += ["", "## Per-run detail", "", "| question | mode | rep | calls | tokens | s | hits | tools |", "|---|---|---:|---:|---:|---:|---:|---|"]
    for x in runs:
        lines.append(f"| {x.question} | {x.mode} | {x.rep} | {x.tool_calls} | {x.tokens} | {x.seconds:.1f} | {x.hits}/{x.truth_n} | {' '.join(x.tools)[:80]} |")
    lines += ["", "## Learned beliefs (automatic, cited)", ""]
    for b in beliefs[:12]:
        lines.append(f"- {b.claim}  \n  citations: {', '.join(b.justified_by)[:200]}")
    lines += ["", "## Answers (first 600 chars)", ""]
    for x in runs:
        lines.append(f"### {x.question} · {x.mode} · rep {x.rep}\n\n{x.answer.strip()}\n")
    Path(args.out).write_text("\n".join(lines) + "\n")
    print(f"\nwrote {args.out}")
    json.dump([x.__dict__ for x in runs], open(Path(args.out).with_suffix(".json"), "w"), indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
