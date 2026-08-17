"""Query engine — the core agentic turn loop for Tether."""
from __future__ import annotations

import json
import os
import random
import re
import sys
import threading
import time
import urllib.error
from dataclasses import dataclass, field
from pathlib import Path

from ..core.config import TetherConfig, CONFIG_DIR
from ..core.logging import get_logger
from ..core.models import Message, StreamEvent, ToolResult, UsageSummary
from ..core.permissions import APPROVAL_DENIED_SIGNAL, PermissionContext
from ..tools.base import ToolRegistry
from ..ui.colors import (
    BOLD, CYAN, DIM, GREEN, MAGENTA, RED, RESET, YELLOW,
    confidence_color, confidence_label, confidence_bar,
)
from ..ui.highlight import highlight_code
from ..ui.markdown import wrap_styled, term_width
from .backend import InferenceBackend, MALFORMED_ARGS_KEY
from .codex_backend import CancelledByUser
from ..core.cache import token_usage_cache


# Short, friendly display labels for tool calls. The raw tool names
# (file_read, file_write) read as noisy boilerplate when a single turn fans
# out into a dozen reads; collapse them to terse verbs so a run scans cleanly.
TOOL_LABELS = {
    "file_read": "read",
    "file_write": "write",
    "file_edit": "edit",
    "glob": "glob",
    "grep": "grep",
    "bash": "bash",
    "agent": "agent",
    "ask_user": "ask",
    "todo_write": "todos",
    "job_list": "jobs",
    "job_output": "job output",
    "job_kill": "stop job",
    "lsp": "lsp",
}

# Tools whose output is pure investigation. The full body still goes to the
# model, but on screen it's noise — so we render each as ONE compact line
# (target + a one-word stat) instead of streaming the name and dumping a
# 200-char preview. This is what kills the "10 file_read previews" wall.
READ_ONLY_DISPLAY = {"file_read", "glob", "grep"}

# max_turns is the hard bound for tool-capable model cycles. Once it is
# exhausted we allow exactly one tool-free model call so the user receives a
# useful checkpoint instead of the old opaque "(max turns reached)" fallback.
TURN_LIMIT_FINALIZATION_PROMPT = """[INTERNAL SAFETY LIMIT — FINAL CHECKPOINT]

You have reached the configured limit of {max_turns} internal model/tool cycles for this submission. Tool use is disabled for this final response.

Give the user a concise, honest checkpoint that says:
1. What was completed or learned.
2. What verification ran and its result, if any.
3. What concrete work remains, if the workflow is unfinished.

Do not claim the task is complete unless the evidence supports that. Do not emit a tool call. If work remains, end by telling the user they can choose Continue workflow to resume from this exact conversation state."""

APPROVAL_DENIED_ERROR_CODE = "APPROVAL_DENIED"
CANCELLED_BEFORE_EXECUTION_ERROR_CODE = "CANCELLED_BEFORE_EXECUTION"
APPROVAL_DENIED_RESPONSE = "I didn’t run that command. What would you like me to do instead?"


def tool_label(name: str) -> str:
    return TOOL_LABELS.get(name, name)


def _read_target(name: str, args: dict) -> str:
    """The one thing a read-only call is *about* — its path or pattern —
    trimmed so the compact line stays on one row."""
    if not isinstance(args, dict):
        return ""
    if name == "file_read":
        target = str(args.get("file_path", ""))
    else:  # glob / grep
        target = str(args.get("pattern", ""))
        path = str(args.get("path", "") or "")
        if path and path != ".":
            target = f"{target} in {path}"
    if len(target) > 64:
        target = "…" + target[-63:]
    return target


def _read_stat(name: str, content: str, is_error: bool) -> str:
    """A terse result summary for a read-only tool — line/match counts, never
    the body itself."""
    body = (content or "").strip()
    if is_error:
        first = body.splitlines()[0] if body else "error"
        return first[:60]
    if name == "file_read":
        if not body or body == "(empty file)":
            return "empty"
        n = body.count("\n") + 1
        return f"{n} line{'s' if n != 1 else ''}"
    # glob / grep
    if (
        body.startswith("No files")
        or body.startswith("No matches")
        or body == "(no matches)"
    ):
        return "no matches"
    n = body.count("\n") + 1
    return f"{n} match{'es' if n != 1 else ''}"


SYSTEM_PROMPT = """You are Tether, a local-first AI coding agent running on the user's machine.
You have access to tools for reading files, editing code, running shell commands, searching codebases, and spawning sub-agents.

Key behaviors:
- Work from the current directory. Desktop sessions enforce the selected project boundary: file tools reject paths outside it, explicit shell paths to outside user data are denied, and bash writes are OS-sandboxed to the project plus temporary storage. Do not inspect or execute user files outside the selected project through bash; system toolchain reads remain available. Terminal sessions may be unrestricted.
- Read files before editing them. Understand existing code before modifying.
- Use bash for git, builds, tests, installing packages, and system commands.
- Use todo_write to keep a concise checklist for multi-step work. Use bash with run_in_background=true for long-lived commands, then job_list/job_output/job_kill to manage them. Prefer lsp for precise definitions, references, hover, or document symbols when a language server is installed.
- Use file_read instead of cat. Use file_edit instead of sed. file_edit supports exact replacements plus line-based edits using start_line/end_line and insert_before_line/insert_after_line. Use glob instead of find. Use grep for content search.
- By default, file reads and file edits are pre-approved. Do not ask for permission in your prose before using file_edit or file_write. Just use the tool when you are ready.
- On every file_edit, include a confidence value (0.0-1.0) reflecting how sure you are the edit is correct and complete. Be honest and calibrated: use low values (below 0.5) when guessing at an unverified API or editing unfamiliar code, high (above 0.8) when the change is mechanical or you confirmed it against the file. This shades the diff so the user reviews risky changes first — do not inflate it.
- For non-obvious lines you add — tricky logic, a deliberate trade-off, a workaround, anything a reviewer might question — include a line_rationale entry on the file_edit: map a distinctive substring of that line to one short sentence explaining why it is the way it is. The user can then ask /why <file>:<line> to see your reasoning. Annotate only lines that genuinely warrant it, not trivial ones.
- Before reading files, write one short sentence naming what you are about to read and why (e.g. "Reading config.py to find the port setting"). One sentence covers a batch of related reads — do not narrate each individual file.
- Default to solving the task yourself in the current thread. Do NOT use the agent tool for simple tasks like editing a README, changing one file, answering a direct question, or making a small focused fix.
- Only consider the agent tool when the user explicitly asks for agents, or when the task is genuinely large enough to benefit from parallel or isolated work. If you want to use an agent without an explicit user request, first ask for approval in a short sentence instead of launching it silently.
- When the user explicitly asks for N agents, use count=N or provide N task entries.
- If the request is ambiguous in a way that would change your approach, call the ask_user tool to put 1-3 quick multiple-choice questions to the user BEFORE you start (e.g. monorepo or standalone, mock the API or hit it, which of two files they meant). Ask up front, not after you have already done work. Only ask when guessing wrong would be costly — never interrogate the user over trivial choices you can reasonably default.
- Be concise. Lead with the action, not the reasoning.
- Only change what was asked. Don't add unnecessary improvements.
- Think through your approach privately. Do not reveal chain-of-thought, long internal reasoning, or deliberation. Give brief progress summaries, decisions, and results only.
- For straightforward documentation edits, read the target file, gather at most one or two supporting facts, then edit. Do not keep paging through the same README unless a specific missing anchor blocks the change.
- For straightforward code changes like adding a focused test, read the target test file or a nearby example once, then make the change. Do not keep exploring once you have a clear insertion point.
- IMPORTANT: If a tool call fails, do NOT retry the same approach. Read the error, diagnose the cause, and try a fundamentally different method. For example, if file_edit fails with a specific mode, try a different edit mode, or use bash with sed/awk, or rewrite the file. Never make the same failing call more than once.
- IMPORTANT: Never call the same tool with the same arguments twice. If you already searched for something and got results, ACT on those results immediately. Do not re-search, re-read, or re-grep for information you already have. Move forward to the next concrete action step.
- IMPORTANT: Do NOT re-read a file after editing it. file_edit returns a diff showing exactly what changed. You already know the new file contents from the edit you just made plus the diff output. Re-reading wastes a turn. Only re-read if you need to see distant, unrelated parts of the file.
- IMPORTANT: After ANY file_edit, your very next assistant message to the user MUST include one plain sentence naming what file was touched and what substantively changed. Example: "Edited tools/file_edit.py: added a SUMMARY line to the result so truncation can't hide it." Do this even if you plan to keep working — one sentence, then continue. The tool result begins with a line starting "SUMMARY:" — use that to anchor your own wording. Do not skip this. Do not replace it with a bare "done" or silence.

Metacognition — self-monitoring your own execution:
- After every few steps, pause and ask yourself: Am I making real progress? Am I closer to completing the user's request than I was 2-3 steps ago?
- Signs you are stuck: you are re-reading files you already read, re-searching for information you already found, re-attempting edits that already failed, or spending multiple turns reasoning without taking concrete action.
- If you catch yourself in a loop: STOP. State explicitly what is blocking you, what you have tried, and what different approach you will take next. Then take that different approach.
- If a tool keeps failing: switch to bash. You can always fall back to shell commands (echo, sed, cat with heredoc, mv, cp) to accomplish file operations.
- Progress means files changed, commands executed, or information gathered that you did not have before. Restating your plan is not progress. Re-reading the same file is not progress. Only new actions count.
- For small requests, take at most 1 short sentence of explanation before acting. Do not produce walls of text.

Skills — reusable procedural knowledge you can load on demand:
- You have a library of skills (proven procedures for recurring tasks). They are NOT in this prompt to save tokens — call skills_list to see what's available (just names + descriptions), then skill_view("<name>") to load the full instructions for one when it applies. Use skill_view("<name>", path="<file>") to read a skill's bundled reference file.
- Check skills_list when a task looks like a known recurring kind of work (debugging, a git/PR workflow, a project-specific procedure) before improvising. Loading a relevant skill is cheap and gives you vetted defaults.
- After you complete a non-trivial workflow — several tool calls, error recovery, or a user correction that taught you something reusable — consider saving it as procedural memory with skill_manage(action="create", ...). Write a clear body (When to Use / Procedure / Pitfalls / Verification). Don't save trivial one-step tasks, and don't duplicate an existing skill (patch it instead). Prefer asking the user first unless they've clearly signalled they want it captured.

Output formatting — your responses are rendered in a terminal with light markdown support:
- You MAY use light markdown and it renders nicely: **bold** for emphasis, `inline code` for identifiers, paths, and commands, # or ## headers for sections, and - bullets or 1. numbered lists for short lists. Keep it light and skim-friendly.
- For multi-line code, use fenced ```language blocks — they are syntax-highlighted. Put single identifiers, file paths, or commands inline in `backticks`.
- Avoid heavy or nested structure: no tables, no deeply nested lists, no raw HTML. Prefer short paragraphs and tight bullets over walls of text.
- Lead with the action or result, not the reasoning. Separate paragraphs with blank lines.

Current working directory: {cwd}
Platform: {platform}
{memory_section}{last_session_section}"""


# Appended to the system prompt only when the persistent codebase model is
# enabled (config.codebase_model_enabled). It teaches the "consult before
# retrieval" habit that is the whole point of the model.
_CODEBASE_MODEL_GUIDANCE = """
Ground every claim about THIS project in the repository. These instructions describe your tools, not the codebase; never describe or assess the project from them. When asked about the project, its design, quality, or "what you think", inspect first (model_query action="architecture", the README, the key modules) and cite what you actually read.

Persistent codebase model — consult it BEFORE grep/read, then verify:
- This repo has a persistent, queryable model of its own structure (an indexed call graph, ownership beliefs, invariants, rejected patterns). It is cheaper and more complete than rediscovering the codebase with grep, so it is your FIRST tool call for any question about callers, impact, ownership, or whether something is allowed.
- Questions like "who calls X", "what does changing X affect", "which files use X": call model_query action="affects" target="<symbol or path>" FIRST — it returns the exact caller list from the indexed call graph (grep finds text matches, not calls, and misses nothing/over-matches). Then read only the specific cited slices you need to verify.
- "who owns / where does X live" → action="owns"; "is X allowed / does a rule forbid X" → action="allowed" (returns recorded invariants and rejected patterns with citations); starting on an unfamiliar area → action="architecture" for the small load-first map. Treat answers as leads and verify the cited slices before you rely on them.
- Before committing or proposing an edit, call model_check (optionally with changed_files) to catch violations of confirmed invariants and reintroduced rejected patterns — a BLOCKING finding means stop and reconsider.
- When you learn something durable and reusable — an ownership fact, an intended invariant, or a pattern the user rejected — record it with model_record so it survives into future sessions. Don't record trivia or things the code already makes obvious.
"""


MEMORY_UPDATE_PROMPT = """Based on our conversation so far, update the persistent memory below.
Keep what's still relevant, remove what's outdated, and add anything new you learned.
Write it as plain text notes — things about the user, their projects, preferences, ongoing work,
decisions made, etc. Keep it concise but complete. This memory persists across all future sessions.

Current memory:
---
{current_memory}
---

Write ONLY the updated memory content, nothing else. No preamble, no explanation."""


PLAN_MODE_DIRECTIVE = """[PLAN MODE ENABLED] You are now operating in plan mode. Slow down and think more deeply than you normally would before doing anything.

1. Take additional time reasoning. Investigate thoroughly first — read the relevant files and search the codebase before forming conclusions. Do not guess at how things work.
2. Consider edge cases, failure modes, and at least one alternative approach. Weigh the trade-offs explicitly rather than committing to the first idea.
3. Then present a clear, concrete, step-by-step plan: what you intend to change, in which files, and why — plus anything you are still unsure about.
4. Do NOT make file edits or run mutating shell commands yet. Read-only investigation (file_read, grep, glob, read-only bash) is encouraged, but hold off on any changes until you have laid the plan out for the user. Wait for them to approve or adjust it before you start executing.

This directive applies only to the current submission. The harness reapplies it
to later submissions while plan mode remains enabled."""

THINKING_PHRASES = [
    "Thinking...",
    "Reasoning...",
    "Analyzing...",
    "Planning approach...",
    "Considering options...",
    "Working through it...",
    "Processing...",
    "Evaluating...",
    "Pondering...",
    "Considering tradeoffs...",
    "Questioning assumptions...",
    "Inspecting context...",
    "Reading the room...",
    "Tracing the problem...",
    "Walking the code...",
    "Following the thread...",
    "Mapping the task...",
    "Sorting signals...",
    "Weighing options...",
    "Checking constraints...",
    "Reviewing evidence...",
    "Connecting pieces...",
    "Exploring paths...",
    "Comparing approaches...",
    "Testing an idea...",
    "Refining the plan...",
    "Looking for the edge case...",
    "Hunting for the bug...",
    "Narrowing it down...",
    "Building a mental model...",
    "Sketching the solution...",
    "Breaking it apart...",
    "Reconstructing intent...",
    "Reviewing the architecture...",
    "Inspecting dependencies...",
    "Checking for regressions...",
    "Looking for a clean path...",
    "Pressure-testing the approach...",
    "Untangling the logic...",
    "Examining the details...",
    "Surveying the codebase...",
    "Working through the states...",
    "Interrogating the output...",
    "Following the evidence...",
    "Checking the invariants...",
    "Estimating the blast radius...",
    "Finding the seam...",
    "Shaping the fix...",
    "Aligning the pieces...",
    "Searching for a simpler route...",
    "Settling on an approach...",
]


class Spinner:
    """Animated spinner for waiting states."""
    FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

    def __init__(self, message: str = "Thinking", stats_fn=None):
        self.message = message
        # Optional live-status callable rendered after the message each frame
        # ("12s · 3.4k tok · 2 tools") so waits aren't a blank stare.
        self.stats_fn = stats_fn
        self._running = False
        self._started = False
        self._thread: threading.Thread | None = None
        self._frame_idx = 0
        self._phrases = random.sample(THINKING_PHRASES, k=len(THINKING_PHRASES))
        self._phrase_idx = 0

    def start(self) -> None:
        self._started = True
        self._running = True
        self._thread = threading.Thread(target=self._spin, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if not self._started:
            return
        self._running = False
        if self._thread:
            self._thread.join(timeout=1)
        # Clear the spinner line
        sys.stderr.write("\r\033[K")
        sys.stderr.flush()

    def update_message(self, message: str) -> None:
        self.message = message

    def _spin(self) -> None:
        while self._running:
            frame = self.FRAMES[self._frame_idx % len(self.FRAMES)]
            if self._frame_idx > 0 and self._frame_idx % 8 == 0:
                self._phrase_idx = (self._phrase_idx + 1) % len(self._phrases)
                self.message = self._phrases[self._phrase_idx]
            suffix = ""
            if self.stats_fn is not None:
                try:
                    stats = self.stats_fn()
                    if stats:
                        suffix = f"  {stats}"
                except Exception:
                    pass
            sys.stderr.write(f"\r{DIM}{frame} {self.message}{suffix}{RESET}\033[K")
            sys.stderr.flush()
            self._frame_idx += 1
            time.sleep(0.1)


class StreamingHighlighter:
    """Streams text to terminal with live syntax highlighting for code blocks.

    Prose lines are printed immediately as they arrive, and code lines print
    as each line completes — highlighted one line at a time so a long block
    streams instead of appearing frozen and dumping at once. Per-line lexing
    can mis-color constructs that span lines (e.g. triple-quoted strings);
    that's the accepted trade-off for liveness.
    """

    def __init__(self, gutter: str = ""):
        self._line_buffer = ""       # partial line accumulator
        self._code_lines: list[str] = []  # buffered code lines
        self._in_code = False
        self._in_fence = False
        self._fence_lang = ""
        self._first_line = True
        self._blank_run: list[str] = []  # blank lines that might be inside a code block
        self._gutter = gutter        # left rail prefixed to prose lines (hierarchy)
        self._gutter_vis = len(re.sub(r"\x1b\[[0-9;]*m", "", gutter))  # visible width

    def feed(self, text: str) -> None:
        """Feed a chunk of streamed text. Prints output incrementally."""
        self._line_buffer += text

        # Process complete lines
        while "\n" in self._line_buffer:
            line, self._line_buffer = self._line_buffer.split("\n", 1)
            self._process_line(line)

    def flush(self) -> None:
        """Flush any remaining buffered content."""
        if self._line_buffer:
            self._process_line(self._line_buffer)
            self._line_buffer = ""
        self._flush_code()
        self._flush_blanks_as_prose()

    def _process_line(self, line: str) -> None:
        stripped = line.strip()

        # Handle markdown fences
        if stripped.startswith("```"):
            if not self._in_fence:
                # Opening fence -- flush any prose blanks, start streaming code
                self._flush_blanks_as_prose()
                self._flush_code()
                self._in_fence = True
                self._fence_lang = stripped.lstrip("`").strip()
                return
            else:
                # Closing fence -- lines already streamed; just reset state
                self._flush_code()
                self._in_fence = False
                self._fence_lang = ""
                return

        if self._in_fence:
            self._print_code_line(line)
            return

        # Detect code vs prose using the existing heuristic
        from ..ui.highlight import _is_code_line

        if _is_code_line(line):
            # Pending blank lines belonged to the code block — print them as code
            for blank in self._blank_run:
                self._print_code_line(blank)
            self._blank_run.clear()
            self._print_code_line(line)
            self._in_code = True
        elif stripped == "" and self._in_code:
            # Blank line while in code -- buffer it (might be mid-block)
            self._blank_run.append(line)
        else:
            # Prose line -- close out any code block first
            self._flush_code()
            self._flush_blanks_as_prose()
            self._print_prose_line(line)

    def _print_code_line(self, line: str) -> None:
        """Highlight and print a single code line immediately."""
        highlighted = highlight_code(line, self._fence_lang).rstrip("\n") if line.strip() else ""
        # Keep the rail going through code, indented under the prose.
        prefix = "\n" if self._first_line else ""
        self._first_line = False
        sys.stdout.write(f"{prefix}{self._gutter}    {highlighted}\n")
        sys.stdout.flush()

    def _flush_code(self) -> None:
        """Reset code-block state (lines now stream as they complete; kept as
        the block-boundary hook for flush() and prose transitions)."""
        self._code_lines.clear()
        self._in_code = False

    def _flush_blanks_as_prose(self) -> None:
        """Print buffered blank lines as prose (they weren't part of a code block)."""
        for line in self._blank_run:
            self._print_prose_line(line)
        self._blank_run.clear()

    def _print_prose_line(self, line: str) -> None:
        raw = line.rstrip()

        # Light block-level handling: headers -> bold, bullets -> •, keep numbers.
        header = re.match(r"^(#{1,6})\s+(.*)$", raw)
        bullet = re.match(r"^(\s*)[-*+]\s+(.*)$", raw)
        numbered = re.match(r"^(\s*\d+\.)\s+(.*)$", raw)
        bold = False
        if header:
            marker, text, bold = "", header.group(2), True
        elif bullet:
            marker, text = bullet.group(1) + "• ", bullet.group(2)
        elif numbered:
            marker, text = numbered.group(1) + " ", numbered.group(2)
        else:
            marker, text = "", raw

        avail = term_width() - self._gutter_vis - len(marker)
        rendered = wrap_styled(text, avail)
        if bold:
            rendered = [f"{BOLD}{r}{RESET}" for r in rendered]

        out = []
        for i, rl in enumerate(rendered):
            pad = marker if i == 0 else " " * len(marker)
            out.append(f"{self._gutter}{pad}{rl}")
        block = "\n".join(out)

        if self._first_line:
            sys.stdout.write(f"\n{block}\n")
            self._first_line = False
        else:
            sys.stdout.write(f"{block}\n")
        sys.stdout.flush()


@dataclass
class TurnResult:
    output: str
    tool_calls_made: list[str] = field(default_factory=list)
    usage: UsageSummary = field(default_factory=UsageSummary)
    stop_reason: str = "completed"


@dataclass
class TaskProgress:
    task_intent: str = "question"
    files_read: set[str] = field(default_factory=set)
    files_written: set[str] = field(default_factory=set)
    bash_commands: list[str] = field(default_factory=list)
    verification_run: bool = False
    real_blocker_seen: bool = False
    agent_calls: int = 0

    @property
    def has_write_action(self) -> bool:
        return bool(self.files_written) or bool(self.bash_commands)

    @property
    def target_file(self) -> str | None:
        if self.files_written:
            return next(iter(self.files_written))
        if self.files_read:
            return next(iter(self.files_read))
        return None


MEMORY_FILE = CONFIG_DIR / "memory.md"


def load_memory() -> str:
    """Load persistent memory from disk."""
    if MEMORY_FILE.exists():
        return MEMORY_FILE.read_text().strip()
    return ""


def save_memory(content: str) -> Path:
    """Save persistent memory to disk."""
    MEMORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    MEMORY_FILE.write_text(content.strip() + "\n")
    return MEMORY_FILE


def _describe_api_error(code: int, body: str, config) -> str:
    """Turn the common provider refusals into one actionable sentence.

    Providers list models in ``/models`` that a given account cannot call
    (Z.AI code 1220, OpenAI ``model_not_found``, Anthropic ``permission``);
    the raw JSON is kept in the log, the user gets told what to change.
    """
    lowered = (body or "").lower()
    model = getattr(config, "api_model", "") or "the selected model"
    provider = getattr(config, "provider", "") or "the provider"
    if code in (401,):
        return (f"Error: {provider} rejected the API key (401). Check the key in the "
                f"runtime sheet or `tether key`.")
    if code in (403, 404) and any(k in lowered for k in (
        "permission", "not have access", "does not exist", "model_not_found",
        "not found", "1220", "unsupported model", "invalid model",
    )):
        return (f"Error: your {provider} account cannot use `{model}` ({code}). "
                f"It is listed by the provider but not enabled for this key/plan — "
                f"pick another {provider} model in the runtime sheet (or `/model`).")
    if code == 429:
        return (f"Error: {provider} rate limit or quota reached (429) for `{model}`. "
                f"Wait a moment or switch models.")
    return ""


class QueryEngine:
    def __init__(
        self,
        config: TetherConfig,
        tool_registry: ToolRegistry,
        permissions: PermissionContext,
        on_approval_request: callable = None,
        on_stream_event: callable = None,
        persistent_context_enabled: bool | None = None,
        include_last_session_summary: bool = True,
    ):
        self.config = config
        self.logger = get_logger(__name__)
        self.backend = InferenceBackend(config)
        self.tools = tool_registry
        self.permissions = permissions
        self.messages: list[Message] = []
        self.usage = UsageSummary()
        self.turn_count = 0
        self.on_approval_request = on_approval_request
        # Presentation-neutral event sink used by the desktop bridge. Terminal
        # rendering remains intact for the CLI; callback failures never break a
        # model turn.
        self.on_stream_event = on_stream_event
        # None preserves the terminal's established behavior. Desktop passes
        # its explicit, persisted opt-in so a fresh app launch does not inherit
        # memory or a prior session summary unless the user enabled it.
        self.persistent_context_enabled = (
            True
            if persistent_context_enabled is None
            else bool(persistent_context_enabled)
        )
        self.include_last_session_summary = bool(include_last_session_summary)
        self._recent_errors: list[tuple[str, str]] = []  # (tool_name, error_msg) — raw
        self._recent_calls: list[tuple[str, str]] = []  # (tool_name, args_fingerprint)
        # Error-class tracking for loop detection that's robust to message
        # drift (varying line numbers, paths, timestamps). Keyed by the
        # normalized class signature, not the raw error text.
        self._recent_error_classes: list[tuple[str, str]] = []  # (tool_name, class_sig)
        # Consecutive-error streak per tool. Reset to 0 on any success.
        # When >= 2, further calls to that tool are blocked pre-execution
        # until the model succeeds with something else.
        self._tool_error_streak: dict[str, int] = {}
        # Number of pre-execution blocks in the current submission.
        # Used to force-terminate the agentic loop when the model keeps
        # slamming into blocks without course-correcting. Reset each
        # submit().
        self._submission_block_count: int = 0
        # When set, the next assistant turn is instructed to respond in
        # plain text only (no tool calls). Reset once honoured.
        self._force_text_response_next: bool = False
        # When set, the current submission is being force-ended — the
        # engine will short-circuit to a final text response and return.
        self._force_end_submission: bool = False
        self._turn_tool_history: list[list[str]] = []  # tool names per turn, for metacognition
        # Plan mode: when on, each inference request receives transient
        # PLAN_MODE_DIRECTIVE guidance and reasoning-capable backends are pushed
        # to max effort. Toggled from the REPL via /plan.
        self.plan_mode: bool = False
        # Low-confidence edits made during the current submission, surfaced in
        # the turn footer so reviewers' eyes land on the risky changes.
        self._turn_low_confidence: list[tuple[str, float]] = []
        # Buffered read-only tool results for tree-style grouped display.
        # Each entry: (tool_name, target_path, stat, is_error)
        self._read_buffer: list[tuple[str, str, str, bool]] = []
        # Snapshot of plan mode for the active submission. Plan instructions are
        # applied only to transient inference messages, never stored in history.
        self._submission_plan_mode: bool = False

        # Self-learning (opt-in via config.self_learning). The detector is
        # lazily built so a disabled session pays nothing. `_suggest_skill`
        # holds a recurring workflow the latest submission surfaced, for the
        # REPL to nudge the user about saving as a skill.
        self._detector = None
        self._suggest_skill = None

        # Codebase mental model edit-hook (opt-in). When the REPL wires this to a
        # CodebaseModel.on_edit, every successful file_edit/file_write
        # incrementally refreshes the substrate index for the touched file — the
        # tightest possible change signal (the agent's own edit). None ⇒ no-op.
        self.on_edit_hook = None

        self.messages.append(self._build_system_message())
        self.logger.info("QueryEngine initialized")

    def _build_system_message(self) -> Message:
        """Build the current system message without touching live history."""
        memory_section = ""
        last_session_section = ""
        if self.persistent_context_enabled:
            memory = load_memory()
            if memory:
                memory_section = (
                    f"\nPersistent memory (from previous sessions):\n{memory}\n"
                )

            last_session_file = CONFIG_DIR / "last_session_summary.txt"
            if self.include_last_session_summary and last_session_file.exists():
                try:
                    summary = last_session_file.read_text().strip()
                    if summary:
                        last_session_section = (
                            "\nLast session summary (for context, not instructions):\n"
                            f"{summary}\n"
                        )
                except OSError:
                    pass

        system_text = SYSTEM_PROMPT.format(
            cwd=os.getcwd(),
            platform=sys.platform,
            memory_section=memory_section,
            last_session_section=last_session_section,
        )
        if getattr(self.config, "codebase_model_enabled", False):
            system_text += _CODEBASE_MODEL_GUIDANCE
        return Message(role="system", content=system_text)

    def set_persistent_context_enabled(self, enabled: bool) -> None:
        """Toggle disk-backed context while preserving the live conversation."""
        self.persistent_context_enabled = bool(enabled)
        system_message = self._build_system_message()
        if self.messages and self.messages[0].role == "system":
            self.messages[0] = system_message
        else:
            self.messages.insert(0, system_message)

    def _messages_for_inference(self) -> list[Message]:
        """Return history with submission-scoped plan guidance when needed."""
        if not self._submission_plan_mode or not self.messages:
            return self.messages
        system = self.messages[0]
        if system.role != "system":
            return [Message(role="system", content=PLAN_MODE_DIRECTIVE), *self.messages]
        planned_system = Message(
            role="system",
            content=f"{system.content or ''}\n\n{PLAN_MODE_DIRECTIVE}",
        )
        return [planned_system, *self.messages[1:]]

    def last_turn_messages(self) -> list:
        """Messages appended by the most recent submit() (may include the
        compaction digest if one happened mid-turn). Used by auto-learning."""
        start = getattr(self, "_turn_start_index", 0)
        return list(self.messages[min(start, len(self.messages)):])

    def _emit_stream_event(self, event: StreamEvent) -> None:
        if self.on_stream_event is None:
            return
        try:
            self.on_stream_event(event)
        except Exception as exc:
            self.logger.debug(f"stream event callback failed: {exc}")

    def _repair_dangling_tool_calls(self, content: str, error_code: str) -> None:
        """Answer every still-pending call from the latest assistant tool batch.

        Cancellation can arrive after some calls in a multi-tool assistant
        message have already produced results.  Looking only at the final
        message misses that partial-batch case and leaves the next provider
        request with an invalid conversation.  Find the newest assistant tool
        batch that still has unanswered IDs and synthesize only the missing
        results.
        """
        for index in range(len(self.messages) - 1, -1, -1):
            message = self.messages[index]
            if message.role == "tool":
                continue
            if message.role != "assistant" or not message.tool_calls:
                return
            answered = {
                later.tool_call_id
                for later in self.messages[index + 1:]
                if later.role == "tool" and later.tool_call_id is not None
            }
            missing = [tc for tc in message.tool_calls if tc.id not in answered]
            if not missing:
                return
            for tc in missing:
                self.messages.append(Message(
                    role="tool",
                    content=content,
                    tool_call_id=tc.id,
                    name=tc.name,
                ))
                self._emit_stream_event(StreamEvent(
                    type="tool_done",
                    tool_name=tc.name,
                    tool_call_id=tc.id,
                    tool_output_preview=content[:240],
                    error_code=error_code,
                ))
            return

    def _cancelled_result(self, all_tool_calls, final_text) -> TurnResult:
        """Abandon the current turn cleanly, keeping the conversation
        well-formed for the next submission:
          - cancelled mid-stream (last msg is the user turn) -> append a
            synthetic assistant reply;
          - cancelled after the model emitted tool_calls we never ran ->
            answer each with a synthetic cancelled tool result, so the next
            turn doesn't see dangling tool_calls.
        """
        last = self.messages[-1] if self.messages else None
        self._repair_dangling_tool_calls(
            "(cancelled by user before execution)",
            CANCELLED_BEFORE_EXECUTION_ERROR_CODE,
        )
        if last is not None and last.role == "user":
            self.messages.append(Message(role="assistant", content="(interrupted by user)"))
        sys.stdout.write(f"\n{YELLOW}Interrupted.{RESET}\n")
        sys.stdout.flush()
        return TurnResult(
            output=final_text or "(interrupted)",
            tool_calls_made=all_tool_calls,
            usage=self.usage,
            stop_reason="cancelled",
        )

    def _approval_denied_result(self, all_tool_calls) -> TurnResult:
        """Stop the turn after an explicit No without another model call."""
        self._flush_read_buffer()
        self._repair_dangling_tool_calls(
            "(cancelled because the user denied approval for this tool batch)",
            APPROVAL_DENIED_ERROR_CODE,
        )
        self.messages.append(Message(role="assistant", content=APPROVAL_DENIED_RESPONSE))
        return TurnResult(
            output=APPROVAL_DENIED_RESPONSE,
            tool_calls_made=all_tool_calls,
            usage=self.usage,
            stop_reason="approval_denied",
        )

    def _workflow_detector(self):
        """The self-learning detector, or None when learning is off. Built
        lazily so toggling /learn auto at runtime takes effect without a
        restart and a disabled session never touches disk."""
        if not getattr(self.config, "self_learning", False):
            return None
        if self._detector is None:
            try:
                from ..core.learning import WorkflowDetector
                self._detector = WorkflowDetector()
            except Exception as e:  # never let learning break a turn
                self.logger.warning(f"self-learning disabled (load failed): {e}")
                self._detector = False
        return self._detector or None

    def submit(self, user_input: str, cancel_event=None) -> TurnResult:
        """Submit user input and run the agentic loop with streaming output.

        When `cancel_event` (a threading.Event) is supplied, the turn is
        abandoned at the next safe point — between SSE chunks, before a model
        call, or before/after tool dispatch — once it is set."""
        self._cancel_event = cancel_event
        self._turn_start_index = len(self.messages)
        self._grounding_nudged = False
        self._submission_plan_mode = bool(self.plan_mode)
        if self._submission_plan_mode:
            # Push reasoning-mode providers to max effort; sampling-only models
            # ignore reasoning_effort, so the directive below carries plan mode
            # for them. The override is read by InferenceBackend._build_payload.
            self.backend.reasoning_effort_override = (
                (self.config.reasoning_effort_max or "max")
                if self.config.reasoning_effort
                else ""
            )
        else:
            self.backend.reasoning_effort_override = ""

        # Reusable knowledge now lives in the skills system: the model loads a
        # relevant skill on its own via skills_list/skill_view. Here we only
        # reset the per-submission self-learning signal; recording happens at
        # the completed return.
        self._suggest_skill = None

        self.messages.append(Message(role="user", content=user_input))
        task_intent = self._classify_task_intent(user_input)
        all_tool_calls = []
        final_text = ""
        # Reset per-submission metacognition state
        self._turn_tool_history = []
        self._recent_calls = []
        self._recent_errors = []
        self._recent_error_classes = []
        self._tool_error_streak = {}
        self._submission_block_count = 0
        self._force_text_response_next = False
        self._force_end_submission = False
        self._metacog_fire_count = 0
        self._metacog_last_fire_turn = -10
        self._metacog_override_announced = False
        self._enforce_write_after_read_budget = False
        self._remaining_targeted_reads = 0
        self._turn_low_confidence = []
        self._read_buffer = []
        self._progress = TaskProgress(task_intent=task_intent)
        # Live-status state for spinner stats (elapsed · tokens · tools)
        self._turn_started_at = time.monotonic()
        self._live_chunk_count = 0

        for turn_idx in range(self.config.max_turns):
            self._turn_tool_count = len(all_tool_calls)
            if cancel_event is not None and cancel_event.is_set():
                self._flush_read_buffer()
                return self._cancelled_result(all_tool_calls, final_text)

            # Metacognition: check if the model is stuck before the next LLM call
            self._metacognition_check(turn_idx, all_tool_calls)

            # Submission-level block cap. If the model has racked up this
            # many pre-execution blocks in a single submission, it is
            # clearly stuck looping against the blocker and every extra
            # turn is wasted. Force a final text response and return.
            BLOCK_CAP = 4
            if self._submission_block_count >= BLOCK_CAP and not self._force_end_submission:
                self._force_end_submission = True
                sys.stdout.write(
                    f"\n{DIM}⚙ the model kept retrying blocked actions — "
                    f"asking it to wrap up in plain text{RESET}\n"
                )
                sys.stdout.flush()
                # Inject a hard instruction that will be the last thing
                # the model sees before its next generation.
                self.messages.append(Message(
                    role="user",
                    content=(
                        f"[SYSTEM INTERRUPT] You have been blocked from executing tools "
                        f"{self._submission_block_count} times in this submission because "
                        f"you keep retrying the same or similar calls. Stop calling tools. "
                        f"Your next response MUST be plain text to the user, describing "
                        f"concretely: (1) what task you were trying to accomplish, "
                        f"(2) what you tried, (3) what's blocking you from proceeding, "
                        f"and (4) what information or decision you need from the user. "
                        f"Do not call any tool. Do not make a plan. Do not apologize. "
                        f"Just describe the state of play and ask for guidance."
                    ),
                ))

            if self.usage.total > self.config.max_budget_tokens:
                self._flush_read_buffer()
                return TurnResult(
                    output=final_text or "(budget limit reached)",
                    tool_calls_made=all_tool_calls,
                    usage=self.usage,
                    stop_reason="max_budget_reached",
                )

            # Proactive compaction: shrink before we hit the limit
            if self.should_compact_proactively():
                ratio = self.get_context_usage_ratio()
                sys.stdout.write(f"\n{YELLOW}Context at {int(ratio * 100)}% — compacting proactively...{RESET}\n")
                self._compact_attempts = 1
                self._compact()
                
                # Show optimization suggestions if any
                suggestions = self.get_optimization_suggestions()
                if suggestions:
                    for suggestion in suggestions:
                        sys.stdout.write(f"{DIM}  💡 {suggestion}{RESET}\n")

            # Show spinner while waiting for first token
            spinner = Spinner("Thinking...", stats_fn=self._turn_stats)
            spinner.start()
            first_token_received = False
            # Live renderer: strips stray markdown and highlights fenced code as
            # it streams, with a dim left rail so the model's prose is visually
            # distinct from indented tool output.
            highlighter = StreamingHighlighter(gutter=f"{DIM}▏{RESET} ")

            def on_thinking_chunk(text: str):
                # Reasoning-mode models (e.g. DeepSeek at reasoning_effort=high)
                # stream a long *private* reasoning trace before any visible text
                # or tool call. We deliberately do NOT stop the spinner here: the
                # reasoning is never printed, so stopping it would leave a frozen,
                # blank screen for the whole think — which reads as a hung harness
                # even though the model is working. Keep it animating until real
                # output (text or a tool call) actually arrives below.
                self._live_chunk_count += 1
                spinner.update_message("Reasoning...")
                # Do not expose private chain-of-thought. The app only needs a
                # phase signal so a slow reasoning model does not look hung.
                self._emit_stream_event(StreamEvent(type="thinking"))

            def on_text_chunk(text: str):
                nonlocal first_token_received
                self._live_chunk_count += 1
                if not first_token_received:
                    first_token_received = True
                    spinner.stop()
                highlighter.feed(text)
                self._emit_stream_event(StreamEvent(type="text", text=text))

            def on_tool_call_start(name: str):
                nonlocal first_token_received
                if not first_token_received:
                    first_token_received = True
                    spinner.stop()
                self._emit_stream_event(StreamEvent(type="tool_running", tool_name=name))
                highlighter.flush()  # print any buffered prose/code before the tool line
                # Read-only calls are rendered as a single compact line when
                # their result lands (see _execute_tool_with_status); printing
                # the label here too would just orphan it above that line.
                if name in READ_ONLY_DISPLAY:
                    return
                sys.stdout.write(f"\n  {MAGENTA}{BOLD}{tool_label(name)}{RESET}")
                sys.stdout.flush()

            def on_tool_call_args_chunk(idx: int, text: str):
                pass

            try:
                assistant_msg, raw_usage = self.backend.chat_completion_stream_parsed(
                    messages=self._messages_for_inference(),
                    tools=self.tools.definitions(),
                    temperature=self.config.temperature,
                    max_tokens=self.config.max_output_tokens,
                    on_text_chunk=on_text_chunk,
                    on_thinking_chunk=on_thinking_chunk,
                    on_tool_call_start=on_tool_call_start,
                    on_tool_call_args_chunk=on_tool_call_args_chunk,
                    cancel_event=cancel_event,
                )
                highlighter.flush()
            except CancelledByUser:
                spinner.stop()
                highlighter.flush()
                self._flush_read_buffer()
                return self._cancelled_result(all_tool_calls, final_text)
            except urllib.error.HTTPError as e:
                spinner.stop()
                self.logger.error(f"HTTP error in LLM call: {e.code} {e.reason}")
                # The backend already consumed the response body (a second
                # e.read() would return b""), so prefer its stashed copy.
                raw_body = getattr(e, "tether_body", None)
                if raw_body is None:
                    raw_body = e.read().decode("utf-8", errors="replace")
                body = raw_body.lower()
                if e.code == 400 and "context" in body:
                    compact_attempts = getattr(self, "_compact_attempts", 0) + 1
                    self._compact_attempts = compact_attempts
                    if compact_attempts <= 3:
                        sys.stdout.write(f"\n{YELLOW}Context overflow — auto-compacting (attempt {compact_attempts}/3)...{RESET}\n")
                        self.logger.warning(f"Context overflow, attempting compaction #{compact_attempts}")
                        self._compact()
                        continue
                    else:
                        sys.stdout.write(f"\n{RED}Context still too large after 3 compaction attempts.{RESET}\n")
                        self.logger.error("Context overflow after 3 compaction attempts")
                        return TurnResult(
                            output="Context overflow: conversation too large even after compaction. Start a new session.",
                            tool_calls_made=all_tool_calls,
                            usage=self.usage,
                            stop_reason="context_overflow",
                        )
                # One clean line + a short untouched snippet; the full body
                # goes to the log, not the conversation.
                snippet = " ".join(raw_body.split())[:160] if raw_body.strip() else ""
                sys.stdout.write(f"\n{RED}✗ API error: {e.code} {e.reason}{RESET}\n")
                if snippet:
                    sys.stdout.write(f"{DIM}  {snippet}{RESET}\n")
                detail = raw_body[:300] if raw_body else "(no response body)"
                self.logger.error(f"LLM API error: {e} - Detail: {detail}")
                self._flush_read_buffer()
                friendly = _describe_api_error(e.code, raw_body, self.config)
                return TurnResult(
                    output=friendly or f"Error: {e}\nDetail: {detail}",
                    tool_calls_made=all_tool_calls,
                    usage=self.usage,
                    stop_reason="error",
                )
            except Exception as e:
                spinner.stop()
                self.logger.error(f"Unexpected error in agent loop: {e}", exc_info=True)
                sys.stdout.write(f"\n{RED}✗ {e}{RESET}\n")
                self._flush_read_buffer()
                return TurnResult(
                    output=f"Error: {e}",
                    tool_calls_made=all_tool_calls,
                    usage=self.usage,
                    stop_reason="error",
                )
            finally:
                if not first_token_received:
                    spinner.stop()

            input_tok = raw_usage.get("prompt_tokens", 0)
            output_tok = raw_usage.get("completion_tokens", 0)
            self.usage = self.usage.add_turn(input_tok, output_tok)
            self.turn_count += 1
            # A successful call means the context fits again; reset the
            # overflow-compaction counter so later overflows get fresh attempts.
            self._compact_attempts = 0
            
            # Cache token usage statistics for optimization
            if self.turn_count % 10 == 0:  # Update cache every 10 turns
                token_usage_cache.set(
                    "token_usage:stats",
                    {
                        'total_tokens': self.usage.total,
                        'average_per_turn': self.usage.total / max(self.turn_count, 1),
                        'turns': self.turn_count
                    },
                    ttl=30.0  # Cache for 30 seconds
                )

            self.messages.append(assistant_msg)

            # Interrupt before running any tools this turn (e.g. a long bash).
            if cancel_event is not None and cancel_event.is_set():
                self._flush_read_buffer()
                return self._cancelled_result(all_tool_calls, final_text)

            # If no tool calls, we're done
            if not assistant_msg.tool_calls:
                final_text = assistant_msg.content or ""
                if (
                    not getattr(self, "_grounding_nudged", False)
                    and self._needs_grounding(user_input, final_text, task_intent, all_tool_calls)
                ):
                    self._grounding_nudged = True
                    sys.stdout.write(f"\n  {YELLOW}[grounding guard: answered about the project without inspecting it]{RESET}\n")
                    sys.stdout.flush()
                    self.messages.append(Message(
                        role="user",
                        content=(
                            "[SYSTEM OVERRIDE — GROUND YOUR ANSWER IN THE REPOSITORY]\n\n"
                            "You answered a question about this project without inspecting a single file. "
                            "Your instructions describe your tools, not this codebase; an assessment based on "
                            "them is a guess.\n\n"
                            "Before answering: call model_query action=\"architecture\" if available, read the "
                            "README and the modules that matter for the question, then answer citing what you "
                            "actually read (file paths). Keep what still holds from your draft; correct the rest."
                        ),
                    ))
                    continue

                if not self._submission_plan_mode and self._should_continue_after_text_response(
                    latest_user_input=user_input,
                    latest_assistant_text=final_text,
                    task_intent=task_intent,
                    all_tool_calls=all_tool_calls,
                ):
                    sys.stdout.write(f"\n  {YELLOW}[completion guard: action requested but no concrete action taken yet]{RESET}\n")
                    sys.stdout.flush()
                    if task_intent == "investigate":
                        override = (
                            "[SYSTEM OVERRIDE — INVESTIGATION STILL REQUIRED]\n\n"
                            "The user pasted content or asked you to look into something. "
                            "You responded with text but did not actually inspect anything.\n\n"
                            "Do not stop here. Your next step should be one of:\n"
                            "1. Read the relevant files with file_read, search with grep, or list with glob.\n"
                            "2. Run a targeted bash command to reproduce or gather more signal.\n"
                            "3. If the paste alone is enough to act on, fix it with file_edit / file_write / bash.\n"
                            "4. If you are genuinely blocked or the user's intent is unclear, ask one specific question instead of restating the paste."
                        )
                    else:
                        override = (
                            "[SYSTEM OVERRIDE — ACTION STILL REQUIRED]\n\n"
                            "The user asked you to make or verify a concrete change. "
                            "You have not taken a write action yet, and you have not clearly explained a real blocker.\n\n"
                            "Do not stop here. Your next step should be one of:\n"
                            "1. Make the change with file_edit, file_write, or bash.\n"
                            "2. Run a targeted verification command if the change is already made.\n"
                            "3. If you are genuinely blocked, explain the blocker plainly instead of pretending the task is done."
                        )
                    self.messages.append(Message(role="user", content=override))
                    continue
                # Flush any remaining buffered reads before the final text
                self._flush_read_buffer()
                sys.stdout.write("\n")
                sys.stdout.flush()
                # Self-learning: record this completed workflow. If it's a
                # recurring one, stash the pattern so the REPL can nudge the
                # user to save it as a skill (the model may also do so itself
                # via skill_manage).
                detector = self._workflow_detector()
                if detector is not None:
                    try:
                        self._suggest_skill = detector.record(
                            user_input, all_tool_calls, os.getcwd(), time.time()
                        )
                    except Exception as e:
                        self.logger.warning(f"self-learning record failed: {e}")
                        self._suggest_skill = None
                return TurnResult(
                    output=final_text,
                    tool_calls_made=all_tool_calls,
                    usage=self.usage,
                    stop_reason="completed",
                )

            # Force-end enforcement: if the block cap fired and the model
            # STILL tried to call tools, refuse to execute them and return
            # whatever text it produced. The goal is to stop wasting turns
            # once we know the model is looping against blocks.
            if self._force_end_submission:
                text_out = (assistant_msg.content or "").strip()
                if not text_out:
                    text_out = (
                        "(submission force-ended by engine: model was blocked "
                        "too many times without course-correcting; please "
                        "restate or clarify the request)"
                    )
                self._flush_read_buffer()
                sys.stdout.write(
                    f"\n{DIM}⚙ wrapping up — {len(assistant_msg.tool_calls)} "
                    f"pending tool call(s) skipped{RESET}\n"
                )
                sys.stdout.flush()
                return TurnResult(
                    output=text_out,
                    tool_calls_made=all_tool_calls,
                    usage=self.usage,
                    stop_reason="force_ended_on_block_cap",
                )

            # Process tool calls — parallelise where safe
            agent_calls = [tc for tc in assistant_msg.tool_calls if tc.name == "agent"]
            other_calls = [tc for tc in assistant_msg.tool_calls if tc.name != "agent"]

            # Max chars for tool output — leave room for system prompt + tools + conversation
            max_tool_chars = min(80000, self.config.context_size * 2)

            # Pre-check all non-agent calls: filter out blocked/duplicate ones first
            # so we know which calls actually need execution.
            read_only_tools = {"grep", "glob", "file_read"}
            parallelisable_tools = {"grep", "glob", "file_read"}
            ready_calls = []  # (tc, call_key) tuples that passed pre-checks

            for tc in other_calls:
                # Malformed-arguments guard. If the model's tool-call arguments
                # could not be parsed as JSON, the backend tags them with
                # MALFORMED_ARGS_KEY instead of inventing parameters. Running the
                # tool with that garbage produces a misleading "missing parameter"
                # error and, after two of them, a per-tool hard block — the exact
                # "raw parameter mode" loop the model gets stuck in. Short-circuit
                # with a precise, recoverable correction so it can simply resend
                # valid JSON without abandoning its approach.
                if isinstance(tc.arguments, dict) and MALFORMED_ARGS_KEY in tc.arguments:
                    raw_snippet = str(tc.arguments[MALFORMED_ARGS_KEY])[:300]
                    corrective = (
                        f"TOOL ARGUMENTS NOT PARSED: the arguments you sent for `{tc.name}` "
                        f"were not valid JSON, so the call was NOT executed. This is a "
                        f"formatting problem, not a failure of the tool or the system — do "
                        f"NOT switch tools or change your approach. Re-issue the same "
                        f"`{tc.name}` call with a single well-formed JSON object: double-quoted "
                        f"keys and string values, and newlines inside strings escaped as \\n. "
                        f"Raw arguments received: {raw_snippet}"
                    )
                    sys.stdout.write(f"  {MAGENTA}{BOLD}{tool_label(tc.name)}{RESET}")
                    sys.stdout.write(f"    {RED}(arguments not valid JSON — asking model to resend){RESET}\n")
                    sys.stdout.flush()
                    # Count toward the submission cap so a model that truly cannot
                    # emit valid JSON still terminates, but don't poison the
                    # per-tool error streak — the tool itself is fine.
                    self._submission_block_count += 1
                    self.messages.append(Message(
                        role="tool", content=corrective,
                        tool_call_id=tc.id, name=tc.name,
                    ))
                    all_tool_calls.append(tc.name)
                    continue

                try:
                    args_fingerprint = json.dumps(tc.arguments, sort_keys=True, default=str)
                except TypeError:  # e.g. mixed-type keys from ast.literal_eval
                    args_fingerprint = repr(tc.arguments)
                call_key = (tc.name, args_fingerprint)
                repeat_call_count = sum(1 for c in self._recent_calls if c == call_key)

                # After metacognition escalation, allow at most one more targeted read
                if (
                    getattr(self, "_enforce_write_after_read_budget", False)
                    and tc.name in read_only_tools
                    and getattr(self, "_remaining_targeted_reads", 0) <= 0
                ):
                    blocked_msg = (
                        "BLOCKED: Your targeted read budget is exhausted until you make a write action. "
                        "Use bash, file_edit, or file_write to make progress. "
                        "If stuck, respond with text explaining what blocks you."
                    )
                    sys.stdout.write(f"  {MAGENTA}{BOLD}{tool_label(tc.name)}{RESET}")
                    sys.stdout.write(f"({DIM}{self._format_args_preview(tc.arguments)}{RESET})\n")
                    sys.stdout.write(f"    {RED}(BLOCKED — read budget exhausted until write action){RESET}\n")
                    sys.stdout.flush()
                    self.messages.append(Message(
                        role="tool", content=blocked_msg,
                        tool_call_id=tc.id, name=tc.name,
                    ))
                    all_tool_calls.append(tc.name)
                    continue

                # Lift enforcement once a write action happens
                if getattr(self, "_enforce_write_after_read_budget", False) and tc.name not in read_only_tools:
                    self._enforce_write_after_read_budget = False
                    self._remaining_targeted_reads = 0

                # HARD BLOCK: refuse a tool that's been failing in a row.
                # Unlike the duplicate-args block below, this catches the
                # "keep trying variations, keep failing" loop. Cleared
                # when the tool succeeds once (see post_process_tool_result).
                streak = self._tool_error_streak.get(tc.name, 0)
                if streak >= 2:
                    # Never enumerate the blocked tool as a recovery option —
                    # the model will read its name in the list and call it again.
                    # Offer exactly two paths: different tool, or text response.
                    blocked_msg = self._build_block_message(
                        tc.name,
                        reason=f"{tc.name} has failed {streak} times in a row",
                        context="The call was NOT executed.",
                    )
                    sys.stdout.write(f"  {MAGENTA}{BOLD}{tool_label(tc.name)}{RESET}")
                    sys.stdout.write(f"({DIM}{self._format_args_preview(tc.arguments)}{RESET})\n")
                    sys.stdout.write(f"    {RED}(BLOCKED — error streak {streak}){RESET}\n")
                    sys.stdout.flush()
                    self._submission_block_count += 1
                    self.messages.append(Message(
                        role="tool", content=blocked_msg,
                        tool_call_id=tc.id, name=tc.name,
                    ))
                    all_tool_calls.append(tc.name)
                    continue

                # HARD BLOCK: refuse to execute duplicate calls entirely.
                # Block on the 2nd identical (tool, args) attempt instead of
                # the 3rd — the previous one-extra-grace-call pattern didn't
                # help the model and just produced one more wasted turn.
                if repeat_call_count >= 1:
                    blocked_msg = self._build_block_message(
                        tc.name,
                        reason=(
                            f"this exact {tc.name} call was already made with "
                            f"identical arguments earlier this submission"
                        ),
                        context="The call was NOT executed — you already have this information.",
                    )
                    sys.stdout.write(f"  {MAGENTA}{BOLD}{tool_label(tc.name)}{RESET}")
                    sys.stdout.write(f"({DIM}{self._format_args_preview(tc.arguments)}{RESET})\n")
                    sys.stdout.write(f"    {RED}(BLOCKED — duplicate call){RESET}\n")
                    sys.stdout.flush()
                    self._submission_block_count += 1
                    self._recent_calls.append(call_key)
                    self.messages.append(Message(
                        role="tool",
                        content=blocked_msg,
                        tool_call_id=tc.id,
                        name=tc.name,
                    ))
                    all_tool_calls.append(tc.name)
                    continue

                # Read-only tools render their whole line (label + target +
                # stat) atomically at result time, so skip the inline args
                # here. Everything else gets its streamed name completed with
                # the argument preview.
                if tc.name not in READ_ONLY_DISPLAY:
                    args_preview = self._format_args_preview(tc.arguments)
                    sys.stdout.write(f"({DIM}{args_preview}{RESET})\n")
                    sys.stdout.flush()

                ready_calls.append((tc, call_key, repeat_call_count))

            # Partition ready calls: parallelisable read-only vs sequential
            # Tools needing approval run sequentially on the main thread.
            parallel_batch = []
            sequential_batch = []
            for tc, call_key, repeat_call_count in ready_calls:
                needs_approval = self.permissions.needs_approval(tc.name)
                if tc.name in parallelisable_tools and not needs_approval:
                    parallel_batch.append((tc, call_key, repeat_call_count))
                else:
                    sequential_batch.append((tc, call_key, repeat_call_count))

            # Execute the parallel batch concurrently
            parallel_approval_denied = False
            if len(parallel_batch) > 1:
                import concurrent.futures

                def _run_tool(tc):
                    return tc, self._gate_and_execute(tc)

                with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(parallel_batch), 8)) as pool:
                    futures = {pool.submit(_run_tool, tc): (tc, ck, rc)
                               for tc, ck, rc in parallel_batch}
                    for future in concurrent.futures.as_completed(futures):
                        tc, call_key, repeat_call_count = futures[future]
                        tool_result = future.result()[1]
                        self._post_process_tool_result(
                            tc, tool_result, call_key, repeat_call_count,
                            max_tool_chars, read_only_tools, all_tool_calls,
                        )
                        if tool_result.error_code == APPROVAL_DENIED_ERROR_CODE:
                            parallel_approval_denied = True
            else:
                # Single call or empty — just run inline
                for tc, call_key, repeat_call_count in parallel_batch:
                    tool_result = self._gate_and_execute(tc)
                    self._post_process_tool_result(
                        tc, tool_result, call_key, repeat_call_count,
                        max_tool_chars, read_only_tools, all_tool_calls,
                    )
                    if tool_result.error_code == APPROVAL_DENIED_ERROR_CODE:
                        parallel_approval_denied = True

            if parallel_approval_denied:
                return self._approval_denied_result(all_tool_calls)
            if cancel_event is not None and cancel_event.is_set():
                self._flush_read_buffer()
                return self._cancelled_result(all_tool_calls, final_text)

            # Flush buffered reads before showing write/action output
            self._flush_read_buffer()

            # Execute sequential batch (writes, bash, anything needing approval)
            for tc, call_key, repeat_call_count in sequential_batch:
                tool_result = self._gate_and_execute(tc)
                self._post_process_tool_result(
                    tc, tool_result, call_key, repeat_call_count,
                    max_tool_chars, read_only_tools, all_tool_calls,
                )
                if tool_result.error_code == APPROVAL_DENIED_ERROR_CODE:
                    return self._approval_denied_result(all_tool_calls)
                if (
                    tool_result.error_code == CANCELLED_BEFORE_EXECUTION_ERROR_CODE
                    or (cancel_event is not None and cancel_event.is_set())
                ):
                    return self._cancelled_result(all_tool_calls, final_text)

            # Flush any remaining buffered reads before agent display
            self._flush_read_buffer()

            # A single approval modal cannot safely service several concurrent
            # waiters. Gate agent launches in model order first, then preserve
            # the useful part of the old behavior by executing every authorized
            # launch in parallel.
            authorized_agent_calls = []
            for tc in agent_calls:
                gate_result = self._gate_tool_call(tc)
                if gate_result is None:
                    authorized_agent_calls.append(tc)
                    continue

                all_tool_calls.append(tc.name)
                self.messages.append(Message(
                    role="tool",
                    content=self._truncate_tool_content(gate_result.content, max_tool_chars),
                    tool_call_id=tc.id,
                    name=tc.name,
                ))
                if gate_result.error_code == APPROVAL_DENIED_ERROR_CODE:
                    return self._approval_denied_result(all_tool_calls)
                if (
                    gate_result.error_code == CANCELLED_BEFORE_EXECUTION_ERROR_CODE
                    or (cancel_event is not None and cancel_event.is_set())
                ):
                    return self._cancelled_result(all_tool_calls, final_text)

            if cancel_event is not None and cancel_event.is_set():
                return self._cancelled_result(all_tool_calls, final_text)

            # Run authorized agent calls in parallel.
            if authorized_agent_calls:
                import concurrent.futures
                agent_approval_denied = False
                if len(authorized_agent_calls) > 1:
                    sys.stdout.write(
                        f"\n  {CYAN}{BOLD}Running {len(authorized_agent_calls)} "
                        f"agents in parallel...{RESET}\n"
                    )
                    sys.stdout.flush()

                def _run_agent(tc):
                    return tc, self._execute_authorized_tool(tc)

                with concurrent.futures.ThreadPoolExecutor(max_workers=self.config.parallel_slots) as pool:
                    futures = {pool.submit(_run_agent, tc): tc for tc in authorized_agent_calls}
                    for future in concurrent.futures.as_completed(futures):
                        tc, tool_result = future.result()
                        all_tool_calls.append(tc.name)
                        self.messages.append(Message(
                            role="tool",
                            content=self._truncate_tool_content(tool_result.content, max_tool_chars),
                            tool_call_id=tc.id,
                            name=tc.name,
                        ))
                        if tool_result.error_code == APPROVAL_DENIED_ERROR_CODE:
                            agent_approval_denied = True

                if agent_approval_denied:
                    return self._approval_denied_result(all_tool_calls)
                if cancel_event is not None and cancel_event.is_set():
                    return self._cancelled_result(all_tool_calls, final_text)

            # Record this turn's tool calls for metacognition tracking
            turn_tools = [tc.name for tc in other_calls] + [tc.name for tc in agent_calls]
            self._turn_tool_history.append(turn_tools)

        # Flush any remaining buffered reads before returning
        self._flush_read_buffer()

        return self._finalize_at_turn_limit(
            all_tool_calls=all_tool_calls,
            cancel_event=cancel_event,
        )

    def _finalize_at_turn_limit(self, all_tool_calls, cancel_event=None) -> TurnResult:
        """Turn a hard agent-loop cutoff into a useful, resumable checkpoint.

        The configured limit remains the hard cap for tool execution. This one
        extra model call receives no tool definitions, so it cannot extend a
        runaway tool loop; it can only summarize progress and remaining work.
        """
        if cancel_event is not None and cancel_event.is_set():
            return self._cancelled_result(all_tool_calls, "")
        limit = max(0, int(self.config.max_turns))
        fallback = self._turn_limit_fallback(limit)
        self.messages.append(Message(
            role="user",
            content=TURN_LIMIT_FINALIZATION_PROMPT.format(max_turns=limit),
        ))
        self._emit_stream_event(StreamEvent(type="checkpoint"))

        spinner = Spinner("Preparing a safe checkpoint...", stats_fn=self._turn_stats)
        spinner.start()
        first_token_received = False
        highlighter = StreamingHighlighter(gutter=f"{DIM}▏{RESET} ")

        def on_thinking_chunk(text: str):
            self._live_chunk_count += 1
            spinner.update_message("Preparing a safe checkpoint...")
            self._emit_stream_event(StreamEvent(type="thinking"))

        def on_text_chunk(text: str):
            nonlocal first_token_received
            self._live_chunk_count += 1
            if not first_token_received:
                first_token_received = True
                spinner.stop()
            highlighter.feed(text)
            self._emit_stream_event(StreamEvent(type="text", text=text))

        try:
            assistant_msg, raw_usage = self.backend.chat_completion_stream_parsed(
                messages=self._messages_for_inference(),
                tools=None,
                temperature=self.config.temperature,
                max_tokens=self.config.max_output_tokens,
                on_text_chunk=on_text_chunk,
                on_thinking_chunk=on_thinking_chunk,
                cancel_event=cancel_event,
            )
            highlighter.flush()
        except CancelledByUser:
            spinner.stop()
            highlighter.flush()
            self._flush_read_buffer()
            return self._cancelled_result(all_tool_calls, "")
        except Exception as exc:
            spinner.stop()
            highlighter.flush()
            self.logger.warning(f"turn-limit checkpoint generation failed: {exc}")
            self.messages.append(Message(role="assistant", content=fallback))
            return TurnResult(
                output=fallback,
                tool_calls_made=all_tool_calls,
                usage=self.usage,
                stop_reason="max_turns_reached",
            )
        finally:
            if not first_token_received:
                spinner.stop()

        input_tok = raw_usage.get("prompt_tokens", 0)
        output_tok = raw_usage.get("completion_tokens", 0)
        self.usage = self.usage.add_turn(input_tok, output_tok)
        self.turn_count += 1
        output = (assistant_msg.content or "").strip() or fallback
        # A provider should not produce tool calls when none were advertised.
        # Store a plain assistant message regardless so the next submission has
        # well-formed history and can resume from this checkpoint.
        self.messages.append(Message(
            role="assistant",
            content=output,
            reasoning_content=assistant_msg.reasoning_content,
        ))
        self._flush_read_buffer()
        sys.stdout.write("\n")
        sys.stdout.flush()
        return TurnResult(
            output=output,
            tool_calls_made=all_tool_calls,
            usage=self.usage,
            stop_reason="max_turns_reached",
        )

    def _turn_limit_fallback(self, limit: int) -> str:
        progress = getattr(self, "_progress", TaskProgress())
        details = []
        if progress.files_written:
            details.append(
                f"changed {len(progress.files_written)} file"
                f"{'s' if len(progress.files_written) != 1 else ''}"
            )
        if progress.files_read:
            details.append(
                f"inspected {len(progress.files_read)} file"
                f"{'s' if len(progress.files_read) != 1 else ''}"
            )
        if progress.verification_run:
            details.append("ran a verification command")
        progress_text = ", ".join(details) if details else "made partial progress"
        return (
            f"Tether reached its {limit}-step safety limit after it {progress_text}. "
            "The workflow may still be unfinished. Choose Continue workflow to resume "
            "from this checkpoint without losing the conversation state."
        )

    def _cancelled_before_execution(self, tc) -> ToolResult:
        return self._emit_unexecuted_tool_result(tc, ToolResult(
            tool_call_id=tc.id,
            name=tc.name,
            content="Tool execution cancelled before it started.",
            is_error=False,
            error_code=CANCELLED_BEFORE_EXECUTION_ERROR_CODE,
        ))

    def _gate_tool_call(self, tc) -> ToolResult | None:
        """Authorize one call, returning a synthetic result when it must not run."""
        self._emit_stream_event(StreamEvent(
            type="tool_running",
            tool_name=tc.name,
            tool_call_id=tc.id,
            tool_args_preview=self._format_args_preview(tc.arguments),
            metadata=self._pending_code_metadata(tc),
        ))
        cancel_event = getattr(self, "_cancel_event", None)
        if cancel_event is not None and cancel_event.is_set():
            return self._cancelled_before_execution(tc)
        denial = self.permissions.blocks(tc.name)
        if denial:
            sys.stdout.write(f"    {RED}denied: {denial.reason}{RESET}\n")
            return self._emit_unexecuted_tool_result(tc, ToolResult(
                tool_call_id=tc.id, name=tc.name,
                content=f"Permission denied: {denial.reason}", is_error=True,
                error_code="PERMISSION_DENIED",
            ))
        if self.permissions.needs_approval(tc.name):
            approval = self._request_approval(tc)
            # Structured clients use an explicit sentinel because their No
            # action also sets the shared token to stop sibling work. Preserve
            # that user intent before treating the token as an ordinary Stop.
            if approval == APPROVAL_DENIED_SIGNAL:
                sys.stdout.write(f"    {YELLOW}skipped by user{RESET}\n")
                return self._emit_unexecuted_tool_result(tc, ToolResult(
                    tool_call_id=tc.id, name=tc.name,
                    content="Tool execution skipped because the user denied approval.", is_error=False,
                    error_code=APPROVAL_DENIED_ERROR_CODE,
                ))
            # An ordinary Stop releases an approval waiter with False too.  The
            # cancellation token is the authoritative distinction: inspect it
            # before interpreting False as an explicit No.  This also closes the
            # Allow-vs-Stop race before any command can begin executing.
            if cancel_event is not None and cancel_event.is_set():
                return self._cancelled_before_execution(tc)
            if approval is False:
                sys.stdout.write(f"    {YELLOW}skipped by user{RESET}\n")
                return self._emit_unexecuted_tool_result(tc, ToolResult(
                    tool_call_id=tc.id, name=tc.name,
                    content="Tool execution skipped because the user denied approval.", is_error=False,
                    error_code=APPROVAL_DENIED_ERROR_CODE,
                ))
            if approval == "ALL":
                self.permissions.grant_session_auto_approve(tc.name)
                sys.stdout.write(
                    f"    {GREEN}auto-approving all {tc.name} calls for the rest of this session{RESET}\n"
                )
                sys.stdout.flush()
            elif isinstance(approval, str):
                # User typed feedback at the approval prompt. Treat this
                # as a hard intervention: the user is trying to redirect.
                # Set the submission-end flag so the engine returns a
                # text response next turn and lets the user restate or
                # resubmit cleanly, rather than letting the model keep
                # dispatching tools as if nothing happened.
                sys.stdout.write(
                    f"    {CYAN}feedback sent — ending submission, "
                    f"next turn must be plain text{RESET}\n"
                )
                sys.stdout.flush()
                self._force_end_submission = True
                feedback_msg = (
                    f"[USER INTERVENTION — feedback at approval]\n"
                    f"The user declined the {tc.name} call and wrote: {approval!r}\n\n"
                    f"Your ONLY next action is a plain-text response to the user: "
                    f"acknowledge the feedback in one sentence, then either "
                    f"(a) restate what you will do differently based on it, or "
                    f"(b) ask one specific follow-up question. "
                    f"Do NOT call any tool. Do NOT retry {tc.name}. The engine "
                    f"will end this submission after you respond."
                )
                return self._emit_unexecuted_tool_result(tc, ToolResult(
                    tool_call_id=tc.id, name=tc.name,
                    content=feedback_msg, is_error=False,
                ))
        if cancel_event is not None and cancel_event.is_set():
            return self._cancelled_before_execution(tc)
        return None

    def _execute_authorized_tool(self, tc) -> ToolResult:
        result = self._execute_tool_with_status(tc)
        return ToolResult(
            tool_call_id=tc.id, name=result.name,
            content=result.content, is_error=result.is_error,
            display_kind=result.display_kind,
            error_code=result.error_code,
            metadata=result.metadata,
        )

    def _gate_and_execute(self, tc) -> ToolResult:
        """Check permissions, request approval if needed, then execute."""
        gated = self._gate_tool_call(tc)
        if gated is not None:
            return gated
        return self._execute_authorized_tool(tc)

    def _emit_unexecuted_tool_result(self, tc, result: ToolResult) -> ToolResult:
        self._emit_stream_event(StreamEvent(
            type="tool_done",
            tool_name=tc.name,
            tool_call_id=tc.id,
            is_error=result.is_error,
            tool_output_preview=result.content[:240],
            display_kind=result.display_kind,
            error_code=result.error_code,
            metadata=result.metadata,
        ))
        return result

    def _execute_tool_with_status(self, tc) -> ToolResult:
        tool = self.tools.get(tc.name)
        if not tool:
            result = ToolResult(
                tool_call_id=tc.id,
                name=tc.name,
                content=f"Unknown tool: {tc.name}",
                is_error=True,
            )
            self._emit_stream_event(StreamEvent(
                type="tool_done", tool_name=tc.name, is_error=True,
                tool_call_id=tc.id,
                tool_output_preview=result.content,
                display_kind=result.display_kind,
                error_code=result.error_code,
                metadata=result.metadata,
            ))
            return result

        # Read-only tools: buffer for tree-style grouped display (flushed
        # when a non-read tool runs or the turn ends).  This turns a noisy
        # wall of flat "read src/foo.py · 42 lines" lines into a compact
        # directory tree that shows the exploration hierarchy.
        if tc.name in READ_ONLY_DISPLAY:
            result = tool.execute_with_cancel(tc.arguments, getattr(self, "_cancel_event", None))
            target = _read_target(tc.name, tc.arguments)
            stat = _read_stat(tc.name, result.content, result.is_error)
            self._read_buffer.append((tc.name, target, stat, result.is_error))
            self._emit_stream_event(StreamEvent(
                type="tool_done", tool_name=tc.name, is_error=result.is_error,
                tool_call_id=tc.id,
                tool_output_preview=result.content[:240],
                display_kind=result.display_kind,
                error_code=result.error_code,
                metadata=result.metadata,
            ))
            return result

        # Show executing status
        spinner = Spinner(f"Running {tool_label(tc.name)}...",
                          stats_fn=getattr(self, "_turn_stats", None))
        spinner.start()
        try:
            result = tool.execute_with_cancel(tc.arguments, getattr(self, "_cancel_event", None))
        finally:
            spinner.stop()

        # Render the result. file_edit is special-cased: we preserve
        # the SUMMARY line and the unified diff with real newlines so
        # the user can actually see what changed instead of a one-line
        # squashed preview. Other tools show the first few real lines.
        color = RED if result.is_error else DIM
        if tc.name == "file_edit" and not result.is_error:
            self._render_file_edit_result(result.content)
        else:
            max_lines = 12 if tc.name == "agent" else 8
            self._render_result_block(result.content, color=color, max_lines=max_lines)

        self._emit_stream_event(StreamEvent(
            type="tool_done", tool_name=tc.name, is_error=result.is_error,
            tool_call_id=tc.id,
            tool_output_preview=result.content[:240],
            display_kind=result.display_kind,
            error_code=result.error_code,
            metadata=result.metadata,
        ))

        return result

    def _flush_read_buffer(self) -> None:
        """Render buffered read-only tool calls as a directory tree.

        Groups file_read entries by parent directory and shows the exploration
        hierarchy with simple indentation. Globs and greps (which have patterns,
        not file paths) are listed as standalone entries.
        """
        if not self._read_buffer:
            return

        entries = self._read_buffer[:]
        self._read_buffer.clear()

        # Separate file-based reads from pattern-based tools
        file_entries: list[tuple[str, str, bool]] = []  # (path, stat, is_error)
        pattern_entries: list[tuple[str, str, str, bool]] = []

        for name, target, stat, is_error in entries:
            if name == "file_read" and target:
                file_entries.append((target, stat, is_error))
            else:
                pattern_entries.append((name, target, stat, is_error))

        # ---- Pattern tools first ----
        for name, target, stat, is_error in pattern_entries:
            label = tool_label(name)
            label_col = RED if is_error else MAGENTA
            detail = f"{DIM} · {stat}{RESET}" if stat else ""
            sys.stdout.write(f"  {label_col}{label}{RESET} {target}{detail}\n")

        if not file_entries:
            sys.stdout.flush()
            return

        # ---- File reads: directory tree ----
        # Sort by path for consistent output, then render by tracking
        # the current directory prefix as we walk sorted paths.
        file_entries.sort(key=lambda e: e[0])

        # Determine if there's a shared prefix worth stripping
        import os as _os
        dirs = {_os.path.dirname(p) for p, _, _ in file_entries}
        # Tool targets may be project-relative or absolute depending on what
        # the model supplied. os.path.commonpath raises ValueError when those
        # styles are mixed (and when Windows paths span drives), so only find a
        # prefix when every directory uses the same path style.
        path_styles = {_os.path.isabs(directory) for directory in dirs}
        try:
            common = (
                _os.path.commonpath(list(dirs))
                if len(dirs) > 1 and len(path_styles) == 1
                else ""
            )
        except ValueError:
            common = ""
        if common in ("", ".", "/"):
            common = ""

        lines: list[str] = []
        # Track the component path we've already rendered
        rendered_parts: list[str] = []

        for path, stat, is_error in file_entries:
            fname = _os.path.basename(path)
            dname = _os.path.dirname(path)
            # Strip common prefix for more compact display
            display_dir = dname[len(common):].lstrip("/") if common and dname.startswith(common) else dname
            # Dropping empty components also keeps an absolute path's leading
            # slash from becoming a blank directory row.
            parts = [
                part for part in display_dir.split(_os.sep)
                if part and part != "."
            ]

            # Find where the path diverges from what we already rendered
            common_len = 0
            for i, (rp, p) in enumerate(zip(rendered_parts, parts)):
                if rp == p:
                    common_len = i + 1
                else:
                    break

            # Print new directory parts
            for i in range(common_len, len(parts)):
                indent = "  " + "  " * i
                lines.append(f"{DIM}{indent}{parts[i]}/{RESET}")
            rendered_parts = parts

            # Print the file
            indent = "  " + "  " * (len(parts) + 1)
            label_col = RED if is_error else DIM
            detail = f"  {DIM}{stat}{RESET}" if stat else ""
            lines.append(f"{indent}{label_col}{fname}{RESET}{detail}")

        sys.stdout.write("\n".join(lines) + "\n")
        sys.stdout.flush()

    @staticmethod
    def _render_result_block(content: str, *, color: str, max_lines: int = 8) -> None:
        """First few lines of a tool result, indented and dimmed — enough to
        see what a command actually printed without scrolling the model's
        conversation away. Long output ends with a "+N more lines" tail."""
        lines = [ln.rstrip() for ln in content.strip().splitlines() if ln.strip()]
        if not lines:
            sys.stdout.write(f"    {color}(no output){RESET}\n")
            sys.stdout.flush()
            return
        width = max(40, term_width() - 6)
        for ln in lines[:max_lines]:
            if len(ln) > width:
                ln = ln[:width - 1] + "…"
            sys.stdout.write(f"    {color}{ln}{RESET}\n")
        if len(lines) > max_lines:
            sys.stdout.write(f"    {DIM}… +{len(lines) - max_lines} more lines{RESET}\n")
        sys.stdout.flush()

    @staticmethod
    def _summary_path(body_lines: list[str]) -> str | None:
        """Pull the edited file path out of a 'SUMMARY: edited <path> via ...' line."""
        prefix = "SUMMARY: edited "
        for ln in body_lines:
            if ln.startswith(prefix):
                return ln[len(prefix):].split(" via ", 1)[0].strip()
        return None

    def _render_file_edit_result(self, content: str) -> None:
        """Pretty-print a file_edit tool result: summary line in green,
        unified diff with +/- lines colorised, trailing note dimmed.

        When the edit carried a self-reported CONFIDENCE marker, shade the
        whole diff by it: a truecolor banner above and a colored gutter rail
        on each body line, so the reviewer's eye lands on low-confidence
        (red) edits instead of rubber-stamping the lot."""
        lines = content.split("\n")

        # Pull out the optional markers (consumed, never printed raw):
        #   CONFIDENCE: <0-1>          -> diff shading
        #   WHY_LINES: <path>|<n,n,n>  -> per-line "why" hint
        confidence = None
        why_path = None
        why_nums = []
        body_lines = []
        for line in lines:
            if confidence is None and line.startswith("CONFIDENCE:"):
                try:
                    confidence = max(0.0, min(1.0, float(line.split(":", 1)[1].strip())))
                except ValueError:
                    pass
                continue
            if line.startswith("WHY_LINES:"):
                payload = line.split(":", 1)[1].strip()
                why_path, _, nums = payload.partition("|")
                why_path = why_path.strip()
                why_nums = [n for n in nums.split(",") if n.strip()]
                continue
            body_lines.append(line)

        if confidence is not None and confidence < 0.5:
            path = why_path or self._summary_path(body_lines) or "?"
            self._turn_low_confidence.append((path, confidence))

        gutter = ""
        if confidence is not None:
            ccol = confidence_color(confidence)
            label = confidence_label(confidence)
            hint = {
                "low": "sketchy — review closely",
                "medium": "worth a second look",
                "high": "looks solid",
            }[label]
            sys.stdout.write(
                f"    {ccol}{BOLD}confidence {confidence:.2f} "
                f"{confidence_bar(confidence)} {label} — {hint}{RESET}\n"
            )
            gutter = f"{ccol}│{RESET} "

        for line in body_lines:
            if line.startswith("SUMMARY:"):
                sys.stdout.write(f"    {GREEN}{BOLD}{line}{RESET}\n")
            elif line.startswith("+++") or line.startswith("---"):
                sys.stdout.write(f"    {gutter}{DIM}{line}{RESET}\n")
            elif line.startswith("+"):
                sys.stdout.write(f"    {gutter}{GREEN}{line}{RESET}\n")
            elif line.startswith("-"):
                sys.stdout.write(f"    {gutter}{RED}{line}{RESET}\n")
            elif line.startswith("@@"):
                sys.stdout.write(f"    {gutter}{CYAN}{line}{RESET}\n")
            elif line.startswith("(Do not re-read"):
                sys.stdout.write(f"    {DIM}{line}{RESET}\n")
            else:
                sys.stdout.write(f"    {gutter}{DIM}{line}{RESET}\n")

        if why_nums and why_path:
            n = len(why_nums)
            sys.stdout.write(
                f"    {MAGENTA}↳ {n} annotated line{'s' if n != 1 else ''} — "
                f"/why {why_path}:<line> for the reasoning{RESET}\n"
            )
        sys.stdout.flush()

    def _post_process_tool_result(
        self, tc, tool_result, call_key, repeat_call_count,
        max_tool_chars, read_only_tools, all_tool_calls,
    ):
        """Shared post-processing for tool results: tracking, error handling, message append."""
        all_tool_calls.append(tc.name)
        was_never_executed = tool_result.error_code in {
            APPROVAL_DENIED_ERROR_CODE,
            CANCELLED_BEFORE_EXECUTION_ERROR_CODE,
        }
        if not was_never_executed:
            self._record_progress(tc.name, tc.arguments, tool_result)
            if tc.name in read_only_tools and getattr(self, "_remaining_targeted_reads", 0) > 0:
                self._remaining_targeted_reads -= 1

        result_content = self._truncate_tool_content(tool_result.content, max_tool_chars)

        if tool_result.is_error:
            # Raw and class-normalized error keys. Raw catches identical
            # text; class catches the same underlying failure across
            # message drift.
            error_key = (tc.name, tool_result.content)
            class_sig = self._error_class_signature(tc.name, tool_result.content)
            class_key = (tc.name, class_sig)
            class_repeat = sum(1 for e in self._recent_error_classes if e == class_key)
            self._recent_errors.append(error_key)
            self._recent_error_classes.append(class_key)
            # Cap both histories so we don't accumulate forever
            if len(self._recent_errors) > 30:
                self._recent_errors = self._recent_errors[-30:]
            if len(self._recent_error_classes) > 30:
                self._recent_error_classes = self._recent_error_classes[-30:]
            # Track the consecutive-error streak for this tool
            self._tool_error_streak[tc.name] = self._tool_error_streak.get(tc.name, 0) + 1
            streak = self._tool_error_streak[tc.name]

            if class_repeat >= 1:
                result_content += (
                    f"\n\nThis is the {class_repeat + 1}th time this call shape has failed "
                    f"with the same class of error ({class_sig!r}). "
                    f"The approach is not working. Your next call MUST use a different tool "
                    f"or fundamentally different arguments — do NOT retry {tc.name} with a "
                    f"small tweak. bash is the universal fallback."
                )
                sys.stdout.write(
                    f"    {YELLOW}(error class repeat #{class_repeat + 1} — switch approach){RESET}\n"
                )
                sys.stdout.flush()
            if streak >= 2:
                result_content += (
                    f"\n\nBLOCKING NOTICE: {tc.name} has now failed {streak} times in a row "
                    f"regardless of arguments. Further calls to {tc.name} will be refused "
                    f"until you succeed with a different tool. Use bash, or respond in text "
                    f"explaining what is blocking you."
                )
        else:
            # Success on this tool clears its failure history so the model
            # can use it again without the pre-check refusing.
            self._recent_errors = [e for e in self._recent_errors if e[0] != tc.name]
            self._recent_error_classes = [
                e for e in self._recent_error_classes if e[0] != tc.name
            ]
            self._tool_error_streak.pop(tc.name, None)

            # Keep the codebase model's substrate fresh on the agent's own edits.
            # The hook (a CodebaseModel.on_edit) is itself crash-proof, but guard
            # here too so a model hiccup never disturbs the turn.
            if self.on_edit_hook is not None and tc.name in ("file_edit", "file_write"):
                path = tc.arguments.get("file_path") if isinstance(tc.arguments, dict) else None
                if path:
                    try:
                        self.on_edit_hook(path)
                    except Exception:
                        pass

        self._recent_calls.append(call_key)
        if len(self._recent_calls) > 30:
            self._recent_calls = self._recent_calls[-30:]
        # Duplicate notice removed: the 2nd identical call is blocked
        # at pre-check, so if we made it here it's the first attempt and
        # there's nothing useful to warn about. The grace-turn "notice,
        # next one will be blocked" pattern just delayed the inevitable
        # block by one wasted turn.

        self.messages.append(Message(
            role="tool",
            content=result_content,
            tool_call_id=tc.id,
            name=tc.name,
        ))

    def _request_approval(self, tc) -> bool:
        if self.on_approval_request:
            return self.on_approval_request(tc.name, tc.arguments)
        return True

    def _metacognition_check(self, turn_idx: int, all_tool_calls: list[str]) -> None:
        """Analyze recent execution history and inject a hard constraint if the model
        appears stuck in a loop or making no forward progress."""
        progress = getattr(self, "_progress", TaskProgress())
        task_intent = progress.task_intent
        min_turns = 3 if task_intent in {"edit", "implement"} else 3
        if turn_idx < min_turns:
            return

        # Throttle: once HARD OVERRIDE has fired, the read budget is already
        # enforced. Re-firing every turn just spams the user with the same
        # banner. Require at least 4 turns between firings.
        last_fire = getattr(self, "_metacog_last_fire_turn", -10)
        if turn_idx - last_fire < 4:
            return

        recent = self._turn_tool_history[-6:]
        if len(recent) < min_turns:
            return

        recent_flat = []
        for turn_tools in recent:
            recent_flat.extend(turn_tools)
        if not recent_flat:
            return

        from collections import Counter
        tool_counts = Counter(recent_flat)
        total_calls = len(recent_flat)

        read_only_tools = {"grep", "glob", "file_read"}
        read_only_count = sum(c for t, c in tool_counts.items() if t in read_only_tools)
        recent_error_count = len(self._recent_errors)

        # Count how many metacognition checks have already fired this submission
        metacog_count = getattr(self, "_metacog_fire_count", 0)

        needs_intervention = False
        reason = ""

        recent_read_calls = [
            call_key for call_key in self._recent_calls[-8:]
            if call_key[0] in read_only_tools
        ]
        repeated_read_calls = len(recent_read_calls) - len(set(recent_read_calls))

        if (
            task_intent in {"edit", "implement"}
            and read_only_count >= 4
            and read_only_count > total_calls * 0.6
            and not progress.has_write_action
            and repeated_read_calls >= 2
        ):
            needs_intervention = True
            reason = (
                f"{read_only_count} of {total_calls} recent calls are read-only, with no write action and repeated reads."
            )
        elif (
            task_intent in {"review", "investigate"}
            and repeated_read_calls >= 3
            and not progress.real_blocker_seen
        ):
            needs_intervention = True
            reason = "Investigative reads are repeating without yielding a clearer blocker or conclusion."
        elif recent_error_count >= 2:
            needs_intervention = True
            reason = f"{recent_error_count} tool errors accumulated. Current approach is failing."
        elif progress.agent_calls >= 2 and not progress.has_write_action and task_intent in {"edit", "implement"}:
            needs_intervention = True
            reason = "Agent delegation is repeating before any concrete local progress."
        elif len(recent) >= 4 and total_calls >= 6:
            patterns = [tuple(t) for t in recent]
            if len(set(patterns)) <= 2:
                needs_intervention = True
                reason = "Tool call pattern is repeating across turns."

        if not needs_intervention:
            return

        metacog_count += 1
        self._metacog_fire_count = metacog_count
        self._metacog_last_fire_turn = turn_idx

        # Gather what the model has already collected from tool results
        progress_lines = [
            f"  intent: {task_intent}",
            f"  files read: {len(progress.files_read)}",
            f"  files written: {len(progress.files_written)}",
            f"  target file: {progress.target_file or '(unknown)'}",
            f"  verification run: {'yes' if progress.verification_run else 'no'}",
            f"  blocker seen: {'yes' if progress.real_blocker_seen else 'no'}",
            f"  agent calls: {progress.agent_calls}",
        ]
        gathered_summary = "\n".join(progress_lines)

        if metacog_count >= 2:
            intervention = (
                f"[SYSTEM OVERRIDE — LOOP DETECTED]\n\n"
                f"You are stuck.\n\n"
                f"Progress state:\n{gathered_summary}\n\n"
                f"You may use at most one more targeted read before you must either make a write action or explain a real blocker.\n"
                f"Prefer file_edit now. If you already have line numbers or exact text, use them. "
                f"If you need to rewrite a section wholesale, use file_edit with start_line/end_line or use bash.\n\n"
                f"If you genuinely cannot proceed, respond with text explaining what is blocking you."
            )
            already_announced_override = getattr(self, "_metacog_override_announced", False)
            self._enforce_write_after_read_budget = True
            self._remaining_targeted_reads = 1
            if not already_announced_override:
                self._metacog_override_announced = True
                sys.stdout.write(f"\n  {DIM}⚙ enough context gathered — steering the model toward acting on it{RESET}\n")
                sys.stdout.flush()
        else:
            intervention = (
                f"[METACOGNITION — PROGRESS CHECK]\n\n"
                f"Observation: {reason}\n\n"
                f"Progress state:\n{gathered_summary}\n\n"
                f"You likely have enough context. "
                f"For edit or implementation work, your next call should usually be a write action. "
                f"For investigation or review, move toward a conclusion instead of repeating broad reads."
            )
            sys.stdout.write(f"\n  {DIM}⚙ progress check: {reason}{RESET}\n")
            sys.stdout.flush()

        self.messages.append(Message(role="system", content=intervention))

    @staticmethod
    def _classify_task_intent(user_input: str) -> str:
        raw = (user_input or "").strip()
        text = raw.lower()
        if not text:
            return "question"

        review_patterns = (
            r"\breview\b",
            r"\baudit\b",
            r"\binspect\b",
            r"\blook\s+over\b",
            r"\bcode\s+review\b",
        )
        question_prefixes = (
            "what", "why", "how", "explain", "summarize", "show me", "tell me",
        )
        investigation_patterns = (
            r"\bfind\b",
            r"\bdebug\b",
            r"\binvestigate\b",
            r"\btrace\b",
            r"\bfigure out\b",
            r"\bwhy is\b",
            r"\bwhat is wrong with\b",
        )
        edit_patterns = (
            r"\bfix\b",
            r"\bupdate\b",
            r"\bedit\b",
            r"\bchange\b",
            r"\brewrite\b",
            r"\brefactor\b",
            r"\bpatch\b",
            r"\bmodify\b",
            r"\bimprove\b",
            r"\bpolish\b",
            r"\btighten\b",
            r"\bclean up\b",
            r"\baddress\b",
            r"\bcorrect\b",
        )
        implement_patterns = (
            r"\bimplement\b",
            r"\bbuild\b",
            r"\bcreate\b",
            r"\badd\b",
            r"\bremove\b",
            r"\brename\b",
            r"\bwire up\b",
            r"\bintegrate\b",
            r"\bmake\s+it\b",
        )

        if any(text.startswith(prefix) for prefix in question_prefixes):
            return "question"
        if any(re.search(pattern, text) for pattern in review_patterns):
            return "review"
        if any(re.search(pattern, text) for pattern in investigation_patterns):
            return "investigate"
        if any(re.search(pattern, text) for pattern in edit_patterns):
            return "edit"
        if any(re.search(pattern, text) for pattern in implement_patterns):
            return "implement"
        # No verb matched. If the input looks like a bare paste of code, an
        # error, or a log, treat it as an investigation request — the user
        # almost always wants the model to do something with the paste, not
        # just acknowledge it.
        if QueryEngine._looks_like_bare_paste(raw):
            return "investigate"
        return "question"

    @staticmethod
    def _looks_like_bare_paste(text: str) -> bool:
        """Heuristic for 'the user pasted content with no instruction'."""
        lines = text.split("\n")
        if len(lines) < 3:
            return False
        lower = text.lower()
        markers = (
            "def ", "class ", "function ", "import ", "from ", "return ",
            "    ", "\t",
            "error:", "exception", "traceback", "stack trace", "stacktrace",
            "warning:", "fatal:", "panic:",
            "{", "}", ";", "=>", "->", "::",
            "://", "/home/", "/usr/", "/var/", "c:\\",
            "<html", "</", "/>",
        )
        hits = sum(1 for m in markers if m in lower)
        return hits >= 2

    @staticmethod
    def _response_describes_blocker(text: str) -> bool:
        lowered = (text or "").lower()
        blocker_markers = (
            "blocked", "cannot", "can't", "unable", "permission denied",
            "missing", "not found", "failed", "need approval", "need access",
            "need the user", "waiting for",
        )
        return any(marker in lowered for marker in blocker_markers)

    _ABOUT_PROJECT_RE = re.compile(
        r"\b(this|the|our|my|your)\s+(project|codebase|code\s*base|repo|repository|code|app|"
        r"application|architecture|design|implementation|source)\b"
        r"|\b(codebase|repository)\b|\bthis repo\b|\bwhat do you think of\b|\bthoughts on\b",
        re.IGNORECASE,
    )

    @staticmethod
    def _needs_grounding(
        latest_user_input: str,
        latest_assistant_text: str,
        task_intent: str,
        all_tool_calls: list[str],
    ) -> bool:
        """A substantive answer *about the project* given without a single tool
        call is almost always the model describing its own instructions (or
        guessing). One nudge to go look; never more than one per submission."""
        if all_tool_calls:
            return False
        if task_intent not in {"question", "review"}:
            return False
        if len((latest_assistant_text or "").strip()) < 200:
            return False  # short replies / clarifying questions are fine
        return bool(QueryEngine._ABOUT_PROJECT_RE.search(latest_user_input or ""))

    @staticmethod
    def _should_continue_after_text_response(
        latest_user_input: str,
        latest_assistant_text: str,
        task_intent: str,
        all_tool_calls: list[str],
    ) -> bool:
        if task_intent not in {"edit", "implement", "investigate"}:
            return False
        if QueryEngine._response_describes_blocker(latest_assistant_text):
            return False
        if task_intent == "investigate":
            # For investigation, read-only tools count as real action.
            concrete_action_tools = {
                "file_read", "grep", "glob", "bash", "file_edit", "file_write",
            }
        else:
            concrete_action_tools = {"file_edit", "file_write", "bash"}
        if any(tool in concrete_action_tools for tool in all_tool_calls):
            return False
        return True

    def _record_progress(self, tool_name: str, arguments: dict, result: ToolResult) -> None:
        progress = getattr(self, "_progress", None)
        if not progress:
            return

        file_path = str(arguments.get("file_path", "")).strip() if isinstance(arguments, dict) else ""
        if tool_name == "file_read" and file_path and not result.is_error:
            progress.files_read.add(file_path)
        elif tool_name in {"file_edit", "file_write"} and file_path and not result.is_error:
            progress.files_written.add(file_path)
        elif tool_name == "bash":
            command = str(arguments.get("command", "")).strip() if isinstance(arguments, dict) else ""
            if command and not result.is_error:
                progress.bash_commands.append(command)
                if self._looks_like_verification_command(command):
                    progress.verification_run = True
        elif tool_name == "agent":
            progress.agent_calls += 1

        if result.is_error and self._response_describes_blocker(result.content):
            progress.real_blocker_seen = True

    @staticmethod
    def _looks_like_verification_command(command: str) -> bool:
        lowered = command.lower()
        verification_markers = (
            "pytest", "unittest", "nosetests", "cargo test", "npm test", "pnpm test",
            "yarn test", "ruff", "mypy", "py_compile", "make test", "go test",
            "gradle test", "jest", "vitest",
        )
        return any(marker in lowered for marker in verification_markers)

    def _compact(self) -> None:
        compact_attempts = getattr(self, "_compact_attempts", 1)

        if len(self.messages) <= 4:
            return

        system = self.messages[0]

        # progressively more aggressive: keep fewer messages each attempt
        keep = max(6, 20 - (compact_attempts - 1) * 6)
        # Walk the split point back past leading tool results so a kept
        # tool message is never orphaned from its assistant tool_calls parent
        # (servers reject that with a 400).
        start = max(1, len(self.messages) - keep)
        while start > 1 and self.messages[start].role == "tool":
            start -= 1
        dropped = self.messages[1:start]
        recent = self.messages[start:]

        # build a digest of dropped messages so context is preserved
        digest = self._build_digest(dropped, compact_attempts)

        # also truncate large content in kept messages
        max_content = 8000 if compact_attempts <= 1 else 3000
        for msg in recent:
            if msg.content and len(msg.content) > max_content:
                half = max_content // 2
                msg.content = (
                    msg.content[:half]
                    + f"\n\n... (compacted: {len(msg.content) - max_content} chars removed) ...\n\n"
                    + msg.content[-half:]
                )

        if digest:
            digest_msg = Message(
                role="user",
                content=f"[Earlier conversation was compacted. Key context preserved below.]\n\n{digest}",
            )
            # need a paired assistant ack so message roles alternate properly
            ack_msg = Message(
                role="assistant",
                content="Understood. I have the context from the compacted conversation and will continue from where we left off.",
            )
            self.messages = [system, digest_msg, ack_msg] + recent
        else:
            self.messages = [system] + recent

    @staticmethod
    def _build_digest(dropped: list[Message], attempt: int) -> str:
        """Extract key context from messages about to be dropped.

        Tool activity is compressed, not discarded: each tool result keeps a
        one-line stub, assistant tool *calls* are named even when the message
        had no prose, and every file the dropped turns read/edited/wrote is
        listed at the end — so after compaction the model still knows what it
        has already looked at and changed, instead of re-reading everything.
        """
        if not dropped:
            return ""

        # budget per message scales down with attempt severity
        user_limit = 400 if attempt <= 1 else 200
        assistant_limit = 600 if attempt <= 1 else 300
        tool_limit = 200 if attempt <= 1 else 100
        total_limit = 6000 if attempt <= 1 else 2500

        parts: list[str] = []
        total_len = 0
        # path -> strongest verb seen ("read" is upgraded to edit/write, never back)
        files_touched: dict[str, str] = {}
        _FILE_VERBS = {"file_read": "read", "file_edit": "edited", "file_write": "wrote"}

        for msg in dropped:
            # Harvest the file inventory from every dropped message, even
            # once the prose budget is spent — the inventory is tiny and is
            # the highest-signal part of the digest.
            for tc in (msg.tool_calls or []):
                args = tc.arguments if isinstance(tc.arguments, dict) else {}
                path = args.get("file_path") or args.get("path")
                verb = _FILE_VERBS.get(tc.name)
                if path and verb:
                    if files_touched.get(path, "read") == "read":
                        files_touched[path] = verb

            if total_len >= total_limit:
                continue

            text = (msg.content or "").strip()
            if msg.role == "user":
                if not text:
                    continue
                if len(text) > user_limit:
                    text = text[:user_limit] + "..."
                entry = f"User: {text}"
            elif msg.role == "assistant":
                calls = ""
                if msg.tool_calls:
                    calls = "called " + ", ".join(tc.name for tc in msg.tool_calls)
                if text and len(text) > assistant_limit:
                    text = text[:assistant_limit] + "..."
                if text and calls:
                    entry = f"Assistant: {text} [{calls}]"
                elif text:
                    entry = f"Assistant: {text}"
                elif calls:
                    entry = f"Assistant: [{calls}]"
                else:
                    continue
            elif msg.role == "tool":
                if not text:
                    continue
                first_line = ""
                for raw in text.splitlines():
                    stripped = raw.strip()
                    if stripped:
                        first_line = stripped
                        break
                if len(first_line) > tool_limit:
                    first_line = first_line[:tool_limit] + "..."
                size_note = f" ({len(text)} chars)" if len(text) > tool_limit else ""
                entry = f"Tool {msg.name or 'result'}{size_note}: {first_line}"
            else:
                continue

            parts.append(entry)
            total_len += len(entry)

        if files_touched:
            inventory = ", ".join(
                f"{path} ({verb})" for path, verb in list(files_touched.items())[:40]
            )
            if len(files_touched) > 40:
                inventory += f", ... and {len(files_touched) - 40} more"
            parts.append(f"Files touched in the compacted span: {inventory}")

        return "\n\n".join(parts) if parts else ""

    @staticmethod
    def _build_block_message(tool_name: str, reason: str, context: str) -> str:
        """Build a tool-result message for a blocked call.

        Key property: NEVER enumerate the blocked tool in the list of
        recovery options. The model reads that list literally and will
        retry the named tool even when it's the one being blocked.

        Offers exactly two concrete recovery paths:
        (a) a *different* tool, chosen by the model based on intent, OR
        (b) a plain-text response to the user explaining what's stuck.

        No third option. No list of tool names.
        """
        return (
            f"BLOCKED: {reason}. {context} "
            f"Do NOT call {tool_name} again in this submission. "
            f"You have exactly two ways forward, pick one now:\n"
            f"  1. Call a substantively different tool — different name, or "
            f"a different kind of operation on different data. Do not retry "
            f"{tool_name} with slightly different arguments.\n"
            f"  2. Respond to the user in plain text, stating concretely what "
            f"is blocked, what you've tried, and what you need from them to "
            f"proceed. Do not call any tool; just respond.\n"
            f"If option 1 is not obvious, choose option 2."
        )

    @staticmethod
    def _error_class_signature(tool_name: str, error_content: str) -> str:
        """Collapse a tool error into a stable class signature.

        We strip the parts that vary between calls to the same underlying
        failure (line numbers, paths, hex addresses, most digits) and keep
        the shape of the first non-empty line. Two errors with different
        line numbers pointing at the same symptom will share a signature.

        Returns a short "<tool>: <normalized-first-line>" string.
        """
        first_line = ""
        for raw in error_content.splitlines():
            stripped = raw.strip()
            if stripped:
                first_line = stripped
                break
        if not first_line:
            return f"{tool_name}: <empty>"
        normalized = first_line
        normalized = re.sub(r"0x[0-9a-fA-F]+", "0xADDR", normalized)
        normalized = re.sub(r"(?:/[A-Za-z0-9_.\-]+)+", "/PATH", normalized)
        normalized = re.sub(r"line\s+\d+", "line N", normalized, flags=re.IGNORECASE)
        normalized = re.sub(r":\d+(?::\d+)?", ":N", normalized)
        normalized = re.sub(r"\b\d{2,}\b", "N", normalized)
        normalized = re.sub(r"\s+", " ", normalized).strip()
        if len(normalized) > 160:
            normalized = normalized[:160]
        return f"{tool_name}: {normalized}"

    @staticmethod
    def _truncate_tool_content(content: str, max_chars: int = 30000) -> str:
        """Truncate tool output to stay within context limits."""
        if len(content) <= max_chars:
            return content
        half = max_chars // 2
        return (
            content[:half]
            + f"\n\n... (truncated {len(content) - max_chars} characters) ...\n\n"
            + content[-half:]
        )

    def _turn_stats(self) -> str:
        """Live status suffix for spinners: "12s · 3.4k tok · 2 tools".

        Token count is the accumulated usage plus a rough count of streamed
        chunks from the in-flight call (~1 token per SSE delta) — a progress
        signal, not an invoice.
        """
        started = getattr(self, "_turn_started_at", None)
        if started is None:
            return ""
        parts = [f"{time.monotonic() - started:.0f}s"]
        total = self.usage.total + getattr(self, "_live_chunk_count", 0)
        if total:
            human = f"{total / 1000:.1f}k" if total >= 1000 else str(total)
            parts.append(f"{human} tok")
        n_tools = getattr(self, "_turn_tool_count", 0)
        if n_tools:
            parts.append(f"{n_tools} tool{'s' if n_tools != 1 else ''}")
        return " · ".join(parts)

    def _estimate_message_tokens(self) -> int:
        """Rough estimate of total tokens in message history (cached)."""
        msg_count = len(self.messages)
        cached = getattr(self, "_token_cache", None)
        if cached and cached[0] == msg_count and cached[1] is self.messages:
            return cached[2]
        total_chars = sum(
            len(m.content or "") + sum(len(str(tc.arguments)) for tc in m.tool_calls)
            for m in self.messages
        )
        result = total_chars // 3  # rough char-to-token ratio
        self._token_cache = (msg_count, self.messages, result)
        return result

    def get_context_usage_ratio(self) -> float:
        """Get the current context usage ratio (0.0 to 1.0+)."""
        estimated = self._estimate_message_tokens()
        return estimated / max(self.config.context_size, 1)

    def should_compact_proactively(self) -> bool:
        """Determine if we should proactively compact based on usage patterns."""
        ratio = self.get_context_usage_ratio()
        # Compact when we're at 75% capacity or higher and have enough messages
        return ratio > 0.75 and len(self.messages) > 6

    def get_optimization_suggestions(self) -> list[str]:
        """Get suggestions for optimizing context usage."""
        suggestions = []
        ratio = self.get_context_usage_ratio()
        
        if ratio > 0.9:
            suggestions.append("Context usage is critical (>90%). Consider starting a new session.")
        elif ratio > 0.8:
            suggestions.append("Context usage is high (>80%). Compaction recommended soon.")
        elif ratio > 0.6:
            suggestions.append("Context usage is moderate (>60%). Monitor for growth.")
        
        # Check for repetitive content
        if len(self.messages) > 10:
            # Simple check for repetitive tool calls
            recent_tool_calls = []
            for msg in self.messages[-10:]:
                recent_tool_calls.extend([tc.name for tc in msg.tool_calls])
            
            if len(recent_tool_calls) > 5:
                from collections import Counter
                tool_counts = Counter(recent_tool_calls)
                most_common = tool_counts.most_common(3)
                if most_common and most_common[0][1] > 3:
                    suggestions.append(f"Frequent tool usage detected: {most_common[0][0]} ({most_common[0][1]} times). Consider batching operations.")
        
        return suggestions

    @staticmethod
    def _pending_code_metadata(tc) -> dict:
        """For write-ish calls, the code about to be written — so the desktop
        can show it the moment the call starts, before the result lands."""
        args = tc.arguments if isinstance(tc.arguments, dict) else {}
        if tc.name == "file_write":
            return {"path": str(args.get("file_path", "")), "code": str(args.get("content", ""))[:40_000],
                    "operation": "write"}
        if tc.name == "file_edit":
            new = args.get("new_string")
            if new is None:
                edits = args.get("edits")
                new = "\n".join(str(e.get("new_string", "")) for e in edits if isinstance(e, dict)) if isinstance(edits, list) else ""
            return {"path": str(args.get("file_path", "")), "code": str(new or "")[:40_000],
                    "old_code": str(args.get("old_string") or "")[:40_000], "operation": "edit"}
        if tc.name == "bash":
            return {"command": str(args.get("command", ""))[:4_000]}
        return {}

    @staticmethod
    def _format_args_preview(args: dict) -> str:
        if not args:
            return ""
        parts = []
        for k, v in args.items():
            val = str(v)
            if len(val) > 60:
                val = val[:57] + "..."
            parts.append(f"{k}={val}")
        return ", ".join(parts)
