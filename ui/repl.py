"""Interactive REPL for Tether."""
from __future__ import annotations

import os
import random
import re
import shlex
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.completion import Completer, Completion, PathCompleter
from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import ANSI, HTML
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.styles import Style

from ..core.config import (
    TetherConfig,
    CONFIG_DIR,
    REMOTE_PROVIDERS,
    apply_provider_selection,
)
from ..core.permissions import PermissionContext
from ..engine.backend import InferenceBackend
from ..engine.query_engine import QueryEngine
from ..engine.turn_controller import TurnController, is_interactive_turn
from ..session.input_broker import InputBroker
from ..session.history import HistoryLog
from ..session.store import StoredSession, list_sessions, load_session, save_session
from ..tools.base import ToolRegistry
from ..tools.agent_tool import AgentTool
from ..tools.ask_user import AskUserTool
from ..session.rationale import RationaleStore
from .banner import print_banner
from .markdown import term_width
from .colors import (
    BOLD, CYAN, DIM, GREEN, MAGENTA, RED, RESET, YELLOW, BLUE,
    bold, dim, error, success, warning,
)


GREETINGS = [
    "What are we building today?",
    "Ready when you are.",
    "Point me at something.",
    "What needs doing?",
    "Where do we start?",
    "Let's get to work.",
    "What's on the board?",
    "Awaiting instructions.",
    "Fire away.",
    "Standing by.",
]

COMPACT_SUMMARY_PROMPT = """Summarize this entire conversation so far in a concise paragraph.
Include: what the user asked for, what was done, what files were changed, any decisions made,
and what the current state is. This summary will replace the conversation history so be thorough
but brief. Write plain text, no markdown."""

SESSION_EXIT_SUMMARY_PROMPT = """Summarize this session in 2-4 sentences for context carryover.
Include: what the user worked on, key changes made, files touched, and any unfinished work.
Write plain text, no markdown. Be specific about file paths and decisions."""

LAST_SESSION_FILE = CONFIG_DIR / "last_session_summary.txt"

ANSI_ESCAPE_RE = re.compile(r"(\x1b\[[0-9;]*m)")
SELF_CHECK_DIR = CONFIG_DIR / "selfcheck"
SELF_CHECK_LATEST = SELF_CHECK_DIR / "latest.txt"
SELF_CHECK_MEMORY_START = "[[selfcheck]]"
SELF_CHECK_MEMORY_END = "[[/selfcheck]]"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _selfcheck_targets() -> list[str]:
    root = _repo_root()
    targets = []
    for pattern in ("tether/**/*.py", "tests/**/*.py"):
        targets.extend(str(path.relative_to(root)) for path in sorted(root.glob(pattern)))
    return targets


def _default_selfcheck_commands(mode: str) -> list[list[str]]:
    commands = [
        [sys.executable, "-m", "py_compile", *_selfcheck_targets()],
        [
            sys.executable,
            "-c",
            "import tether, tether.cli, tether.ui.repl, tether.engine.query_engine",
        ],
    ]
    if mode == "deep":
        commands.append([
            sys.executable,
            "-m",
            "unittest",
            "tests.test_query_engine_robustness",
            "tests.test_file_edit_tool",
        ])
    return commands


def _format_selfcheck_report(mode: str, commands: list[list[str]], outputs: list[tuple[int, str, str]]) -> str:
    timestamp = datetime.now().isoformat(timespec="seconds")
    status = "PASS" if all(code == 0 for code, _, _ in outputs) else "FAIL"
    lines = [
        f"Selfcheck: {status}",
        f"Time: {timestamp}",
        f"Mode: {mode}",
        f"CWD: {os.getcwd()}",
        "",
    ]
    for command, (returncode, stdout, stderr) in zip(commands, outputs):
        lines.append(f"$ {' '.join(shlex.quote(part) for part in command)}")
        lines.append(f"exit_code: {returncode}")
        if stdout.strip():
            lines.append("STDOUT:")
            lines.append(stdout.rstrip())
        if stderr.strip():
            lines.append("STDERR:")
            lines.append(stderr.rstrip())
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _summarize_selfcheck_for_memory(report: str) -> str:
    lines = [line.rstrip() for line in report.splitlines()]
    status = next((line.split(":", 1)[1].strip() for line in lines if line.startswith("Selfcheck:")), "UNKNOWN")
    time_line = next((line.split(":", 1)[1].strip() for line in lines if line.startswith("Time:")), "")
    command = next((line[2:] for line in lines if line.startswith("$ ")), "")
    stderr_lines: list[str] = []
    collecting_stderr = False
    for line in lines:
        if line == "STDERR:":
            collecting_stderr = True
            continue
        if collecting_stderr and (not line or line.startswith("$ ") or line.startswith("exit_code:")):
            break
        if collecting_stderr:
            stderr_lines.append(line)
    detail = " | ".join(stderr_lines[:3]).strip()
    if not detail:
        detail = "no stderr captured"
    summary = [
        SELF_CHECK_MEMORY_START,
        f"Recent selfcheck status: {status}",
        f"Time: {time_line}",
    ]
    if command:
        summary.append(f"Command: {command}")
    summary.append(f"Detail: {detail}")
    summary.append(SELF_CHECK_MEMORY_END)
    return "\n".join(summary)


def _upsert_selfcheck_memory(current_memory: str, report: str) -> str:
    new_block = _summarize_selfcheck_for_memory(report)
    lines = current_memory.splitlines()
    filtered: list[str] = []
    skipping = False
    for line in lines:
        if line.strip() == SELF_CHECK_MEMORY_START:
            skipping = True
            continue
        if skipping and line.strip() == SELF_CHECK_MEMORY_END:
            skipping = False
            continue
        if not skipping:
            filtered.append(line)
    base = "\n".join(filtered).strip()
    return f"{base}\n\n{new_block}\n".strip() + "\n"


def _build_selfcheck_context_message(report_path: Path, report: str) -> str:
    preview = "\n".join(report.strip().splitlines()[:20])
    return (
        "[Selfcheck result for the current tether codebase]\n"
        f"Report: {report_path}\n\n"
        "Use this as execution feedback from a separate verification run.\n"
        "If the selfcheck failed, fix the code against the current files on disk.\n\n"
        f"{preview}"
    )


def readline_safe_prompt(prompt: str) -> str:
    """Mark ANSI escapes as non-printing so readline keeps cursor position correct."""
    return ANSI_ESCAPE_RE.sub(lambda match: f"\001{match.group(1)}\002", prompt)


def _build_key_bindings() -> KeyBindings:
    """Enter submits, except when more keys are queued behind it (a paste in
    flight). Alt+Enter and Ctrl+J always insert a literal newline."""
    kb = KeyBindings()

    @kb.add("enter")
    def _enter(event):
        # If the input parser still has bytes queued behind this Enter, the
        # terminal handed us a multi-byte read — i.e. a paste. Treat the
        # Enter as a literal newline so the rest of the paste lands in the
        # same buffer instead of submitting one line at a time. This is the
        # fallback for terminals that don't honor bracketed-paste mode.
        if event.app.key_processor.input_queue:
            event.current_buffer.insert_text("\n")
        else:
            event.current_buffer.validate_and_handle()

    @kb.add("escape", "enter")
    def _newline_alt(event):
        event.current_buffer.insert_text("\n")

    @kb.add("c-j")
    def _newline_ctrl_j(event):
        event.current_buffer.insert_text("\n")

    @kb.add("/")
    def _slash_menu(event):
        # Insert the slash, then force the command palette open when "/" begins
        # the line. complete_while_typing does NOT reliably auto-open the menu on
        # the first "/" in every terminal, so we trigger it explicitly — this is
        # what gives the instant slash dropdown (like Claude Code / Hermes).
        # Guarded so a "/" keystroke can never fail the input, even if a
        # prompt_toolkit version quirks on start_completion.
        b = event.current_buffer
        b.insert_text("/")
        if b.document.text_before_cursor.lstrip() == "/":
            try:
                b.start_completion(select_first=False)
            except Exception:
                pass

    return kb


def format_tool_args(args: dict) -> str:
    if not args:
        return ""
    parts = []
    for k, v in args.items():
        val = str(v)
        if len(val) > 80:
            val = val[:77] + "..."
        parts.append(f"{k}={val}")
    return ", ".join(parts)


def request_approval(tool_name: str, arguments: dict) -> bool | str:
    """Returns True (approve), False (deny), 'ALL' (auto-approve this tool for the rest of the session), or a string (user feedback)."""
    print(f"\n  {YELLOW}{BOLD}Approval required{RESET}")
    if tool_name == "agent":
        print(f"  {DIM}The model wants to launch a sub-agent instead of continuing directly.{RESET}")
    elif tool_name == "bash":
        print(f"  {DIM}The model wants to run a shell command on your machine.{RESET}")
    print(f"  {MAGENTA}{tool_name}{RESET}({format_tool_args(arguments)})")
    try:
        prompt = readline_safe_prompt(
            f"  {DIM}Enter y, n, a (yes for all {tool_name} this session), or feedback:{RESET} "
        )
        resp = input(prompt).strip()
        lower = resp.lower()
        if lower in ("a", "all", "yes for all", "yes-for-all", "ya"):
            return "ALL"
        if lower in ("y", "yes"):
            return True
        if lower in ("n", "no", ""):
            return False
        return resp
    except (EOFError, KeyboardInterrupt):
        return False


def resolve_choice(resp: str, options: list[str]) -> str:
    """Map a user's reply to a clarifying question onto an option: a bare number
    selects that option, an empty reply takes the first, anything else is a
    custom free-form answer."""
    resp = resp.strip()
    if resp.isdigit():
        idx = int(resp) - 1
        if 0 <= idx < len(options):
            return options[idx]
    if not resp:
        return options[0] if options else ""
    return resp


def _human_tokens(n: int) -> str:
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def paste_summary(text: str) -> str | None:
    """A one-line summary for multi-line (pasted) input, or None for short
    input. Keeps a big paste from scrolling the prompt off-screen."""
    line_count = text.count("\n") + 1
    if line_count <= 5:
        return None
    first = text.split("\n", 1)[0]
    preview = first[:60] + ("..." if len(first) > 60 else "")
    return f"  {DIM}pasted {line_count} lines{RESET} {DIM}{preview}{RESET}"


def classify_input(raw_line: str, busy: bool, has_pending: bool):
    """Decide what a typed line means given REPL state. Pure and testable.

    Returns (action, payload):
      fulfill    deliver payload (the raw line) to a pending broker request
      submit     start a new turn with payload
      also       add payload as a follow-up (/also; queued if a turn is running)
      redirect   cancel the current turn and run payload next
      interrupt  stop the current turn
      slash      run payload as a normal slash command (only when idle)
      slash_busy a normal slash command typed mid-turn (rejected)
      noop       nothing to do
    """
    if has_pending:
        return ("fulfill", raw_line)
    line = raw_line.strip()
    if not line:
        return ("noop", None)
    if line.startswith("/"):
        parts = line.split(maxsplit=1)
        cmd = parts[0].lower()
        rest = parts[1].strip() if len(parts) > 1 else ""
        if cmd == "/also":
            return ("also", rest) if rest else ("noop", None)
        if cmd == "/redirect":
            return ("redirect", rest) if rest else ("noop", None)
        if cmd in ("/stop", "/interrupt"):
            return ("interrupt", None)
        return ("slash_busy", line) if busy else ("slash", line)
    # plain prose: a new turn when idle, an /also follow-up while busy
    return ("also", line) if busy else ("submit", line)


SLASH_COMMANDS = [
    "/help", "/plan", "/why", "/clear", "/compact", "/history", "/usage",
    "/context", "/session", "/cd", "/save", "/resume", "/memory", "/selfcheck",
    "/summaries", "/provider", "/model", "/learn", "/skills", "/persistence",
    "/also", "/redirect", "/stop", "/quit", "/exit",
]

# One-line descriptions shown to the right of each command in the completion
# menu (like the harness's slash palette).
SLASH_HELP = {
    "/help": "Show all commands", "/plan": "Toggle plan mode",
    "/why": "Reasoning for an edited line", "/clear": "Clear the context",
    "/compact": "Summarize + trim context", "/history": "Event history",
    "/usage": "Token usage", "/context": "Context-window usage",
    "/session": "Session info", "/cd": "Change directory",
    "/save": "Save session to disk", "/resume": "Restore a saved conversation",
    "/memory": "Persistent memory",
    "/selfcheck": "Run a self-check", "/summaries": "Conversation summaries",
    "/provider": "Switch model provider", "/learn": "Author a skill",
    "/skills": "List / show / forget skills",
    "/model": "LLM model & token usage", "/persistence": "Codebase mental model",
    "/also": "Queue a follow-up mid-turn", "/redirect": "Steer the current turn",
    "/stop": "Interrupt the turn", "/quit": "Exit", "/exit": "Exit",
}

# Subcommands surfaced as flat completions ("/persistence build") with their own
# descriptions, so the menu lists them the way the harness does.
SLASH_SUBCOMMANDS = {
    "/model": [("stats", "Show the current model"), ("local", "Switch to local GGUF"),
               ("api", "Switch to your API model")],
    "/persistence": [("status", "Model status"), ("build", "Full re-index"),
               ("sync", "Incremental refresh"), ("check", "Run invariants vs. diff"),
               ("ask", "Query the model"), ("gc", "Remove orphaned model DBs")],
    "/skills": [("show", "Show a skill"), ("forget", "Delete a user skill"),
                ("doctor", "Check skill files for problems")],
    "/learn": [("auto", "Toggle auto-learning"), ("this chat", "Skill from this chat")],
    "/memory": [("show", "Show memory"), ("clear", "Clear memory")],
    "/plan": [("on", "Force plan on"), ("off", "Force plan off")],
}


class TetherCompleter(Completer):
    """Tab-completion for the REPL: slash commands and their subcommands (with
    descriptions), plus path arguments for the commands that take one
    (/cd -> directories, /why -> files)."""

    def __init__(self, skill_store=None):
        self._dirs = PathCompleter(expanduser=True, only_directories=True)
        self._files = PathCompleter(expanduser=True)
        self._skill_store = skill_store

    def _skill_commands(self) -> list[str]:
        if self._skill_store is None:
            return []
        try:
            reserved = {c.lstrip("/") for c in SLASH_COMMANDS}
            # Bundles win on slug collision, so list them first and de-dupe.
            slugs = [b.slug for b in self._skill_store.bundles()]
            slugs += [s.slug for s in self._skill_store.all()]
            seen, out = set(), []
            for slug in slugs:
                if slug in reserved or slug in seen:
                    continue
                seen.add(slug)
                out.append(f"/{slug}")
            return out
        except Exception:
            return []

    def _command_entries(self) -> list[tuple[str, str]]:
        """(completion_text, description) for every command, including flat
        subcommand variants ('/persistence build'), plus installed skills."""
        entries: list[tuple[str, str]] = []
        for cmd in SLASH_COMMANDS:
            entries.append((cmd, SLASH_HELP.get(cmd, "")))
            for sub, desc in SLASH_SUBCOMMANDS.get(cmd, []):
                entries.append((f"{cmd} {sub}", desc))
        for sc in self._skill_commands():
            entries.append((sc, "skill"))
        return entries

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        if not text.startswith("/"):
            return
        # A path-argument command delegates to a PathCompleter once its arg starts.
        head, _, _ = text.partition(" ")
        sub_completer = {"/cd": self._dirs, "/why": self._files}.get(head)
        if sub_completer is not None and " " in text:
            arg = text[len(head):].lstrip()
            sub = Document(arg, len(arg))
            yield from sub_completer.get_completions(sub, complete_event)
            return
        # Otherwise complete command names and their subcommands, matching the
        # whole typed text so "/persistence" surfaces "/persistence build" etc.
        # At a bare "/" we keep the menu to top-level commands (uncluttered);
        # once the user types more, subcommand variants appear.
        bare = text == "/"
        for entry_text, meta in self._command_entries():
            if bare and " " in entry_text:
                continue
            if entry_text.startswith(text):
                yield Completion(entry_text, start_position=-len(text),
                                 display_meta=meta)


def ask_clarifying_questions(questions: list[dict]) -> list[dict]:
    """Render the model's multiple-choice clarifying questions in the terminal
    and collect the user's answers. Returns a list of {"question", "answer"}.

    A bare number selects that option; anything else is taken as a free-form
    custom answer; an empty line falls back to the first option. Raises on
    EOF/Ctrl-C so AskUserTool can convert it into a "proceed with defaults"
    result rather than blocking the run."""
    print(f"\n  {CYAN}{BOLD}A few quick questions before I start:{RESET}")
    answers: list[dict] = []
    for i, q in enumerate(questions, 1):
        text = q.get("question", "")
        options = q.get("options", [])
        print(f"\n  {BOLD}{i}. {text}{RESET}")
        for j, opt in enumerate(options, 1):
            print(f"     {MAGENTA}{j}{RESET}) {opt}")
        print(f"     {DIM}(enter a number, or type your own answer){RESET}")
        prompt = readline_safe_prompt(f"  {DIM}>{RESET} ")
        resp = input(prompt)
        answers.append({"question": text, "answer": resolve_choice(resp, options)})
    print()
    return answers


def context_indicator(engine: QueryEngine, config: TetherConfig) -> str:
    """Short context usage indicator — only shown when usage is notable."""
    estimated_tokens = engine._estimate_message_tokens()
    max_tokens = config.context_size
    pct = min(100, int((estimated_tokens / max_tokens) * 100))

    if pct < 25:
        return ""

    if pct < 50:
        color = DIM
    elif pct < 75:
        color = YELLOW
    else:
        color = RED

    return f" {color}ctx {pct}%{RESET}"


class TetherREPL:
    def __init__(self, config: TetherConfig):
        self.config = config
        self.permissions = PermissionContext.from_config(config)
        self.tool_registry = ToolRegistry.build_default(
            include_codebase_model=getattr(config, "codebase_model_enabled", False))
        self.history = HistoryLog()
        self.session = StoredSession.new(cwd=os.getcwd())

        agent_tool = AgentTool(config, self.tool_registry, self.permissions)
        self.tool_registry.tools["agent"] = agent_tool

        # Mediates the single keyboard between the main loop and a background
        # turn that needs input (tool approval, clarifying questions).
        self._broker = InputBroker()
        self._controller: TurnController | None = None
        self._engine: QueryEngine | None = None
        self._busy_hint_shown = False  # show the type-ahead hint once per session
        self._last_total = 0           # token total at last turn, for per-turn delta

        # Reverse clarifying questions: the model can call ask_user to put 1-3
        # multiple-choice questions to the user before committing to an approach.
        # Answers are collected through the broker so a background turn can ask.
        self.tool_registry.tools["ask_user"] = AskUserTool(ask_fn=self._ask_clarifying)

        # Skills: progressive-disclosure knowledge docs. build_default already
        # wired the loader/authoring tools onto the registry against the shared
        # store; grab that same store for REPL-side use (slash exposure,
        # /skills, /learn) so every reader sees one fresh scan.
        from ..core.skills import default_store
        self.skill_store = default_store()

        # Persistent codebase mental model (opt-in). Resolve the per-repo model
        # so the REPL can build/sync it and wire the edit-hook; the tools were
        # registered above against the same get_model singleton. Lazy + guarded
        # so a disabled or broken model never blocks startup.
        self.codebase_model = None
        if getattr(config, "codebase_model_enabled", False):
            try:
                from ..core.codebase_model.service import _resolve_root, get_model
                root = _resolve_root(os.getcwd())
                if root == Path.home():
                    # Launched from ~ with no enclosing git repo: a "codebase
                    # model" of the whole home directory means walking and
                    # hashing everything under it at startup — never intended.
                    print(dim("Mental model off: cwd is your home directory, "
                              "not a project. cd into a repo (or /cd) to use it."))
                else:
                    self.codebase_model = get_model(os.getcwd())
                    self.codebase_model.beliefs.max_beliefs = getattr(
                        config, "codebase_model_max_beliefs", 500)
                    self.codebase_model.indexer.max_files = getattr(
                        config, "codebase_model_max_files", 10000)
            except Exception as e:  # never let the model break the REPL
                print(warning(f"codebase model unavailable: {e}"))
                self.codebase_model = None

        # Per-line "why": file_edit records line rationales here so the user can
        # ask /why <file>:<line> during review.
        self.rationale_store = RationaleStore()
        file_edit_tool = self.tool_registry.get("file_edit")
        if file_edit_tool is not None:
            file_edit_tool.rationale_store = self.rationale_store

        self._toolbar_style = Style.from_dict({
            # Quiet status text, no reverse-video block — it's a caption,
            # not a banner. noreverse beats prompt_toolkit's default.
            "bottom-toolbar": "noreverse noinherit #6c7086",
            "frame.border": "#585b70",  # muted single-line input frame
        })
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            history = FileHistory(str(CONFIG_DIR / "history"))
        except OSError:
            history = None
        self._history = history
        self._prompt_session = PromptSession(
            multiline=True,
            key_bindings=_build_key_bindings(),
            style=self._toolbar_style,
            history=history,
            auto_suggest=AutoSuggestFromHistory(),
            completer=TetherCompleter(skill_store=self.skill_store),
            complete_while_typing=True,
        )
        self.last_selfcheck_report: Path | None = None

    def run(self, resume: str = "") -> int:
        """Run the REPL. ``resume`` restores a saved conversation first:
        "@latest" (or "") from --continue picks the most recent session,
        anything else is treated as a session id."""
        print_banner()

        backend = InferenceBackend(self.config)
        if not backend.health_check():
            if self.config.is_remote:
                print(error(f"Cannot reach remote API at {self.config.server_url}"))
                print(dim(f"Check network and ${self.config.api_key_env}"))
            else:
                print(error(f"Cannot reach server at {self.config.server_url}"))
                print(dim("Start it with: tether serve"))
            return 1

        # Greeting
        now = datetime.now()
        hour = now.hour
        if hour < 12:
            time_greeting = "Good morning"
        elif hour < 17:
            time_greeting = "Good afternoon"
        else:
            time_greeting = "Good evening"

        self._print_welcome(time_greeting)

        engine = QueryEngine(
            config=self.config,
            tool_registry=self.tool_registry,
            permissions=self.permissions,
            on_approval_request=self._approval,
        )
        self._engine = engine

        # Codebase model: keep the substrate fresh on the agent's own edits. On an
        # already-built store, do a cheap incremental sync; on a fresh repo, build
        # it automatically the first time (with a progress bar) so the user never
        # has to remember /persistence build. Either way, failures degrade to a
        # hint — /persistence sync is the manual fallback.
        if self.codebase_model is not None:
            engine.on_edit_hook = self._model_on_edit
            # Stale beliefs consulted mid-session get a real LLM check against
            # their refetched citations instead of trust-on-refetch.
            try:
                from ..core.codebase_model.service import set_default_verifier
                from ..core.codebase_model.verify import llm_verifier
                set_default_verifier(llm_verifier(engine.backend))
            except Exception:
                pass  # verification is an enhancement, never a startup blocker
            try:
                if self.codebase_model.store.count_nodes() > 0:
                    self.codebase_model.sync()  # cheap, near-instant
                else:
                    print(dim("First run in this repo — building the mental model "
                              "(one time; it stays fresh as you work):"))
                    self._model_build_with_bar(self.codebase_model)
            except Exception as e:
                print(warning(f"Mental model auto-build skipped: {e}. "
                              f"Run /persistence sync to build it."))

        # --continue / --resume from the CLI: restore before the first prompt.
        if resume:
            target = resume
            if resume in ("", "@latest"):
                recent = [s for s in list_sessions()
                          if s.messages and s.session_id != self.session.session_id]
                target = recent[0].session_id if recent else ""
                if not target:
                    print(dim("No previous conversation to continue — starting fresh."))
            if target:
                self._handle_resume(["/resume", target], engine)

        # Turns run on a background worker so the user can keep typing during
        # generation — /also to queue, /redirect or Esc/Ctrl-C to interrupt.
        controller = TurnController(
            engine,
            on_error=lambda exc: print(error(f"Error: {exc}")),
            on_turn_complete=self._on_turn_complete,
        )
        self._controller = controller

        # Idle uses the rich prompt_toolkit prompt. While a turn runs we hand the
        # terminal back to the engine so tokens stream straight to stdout (no
        # repaint flicker, correct ordering) and poll stdin for type-ahead in
        # _run_busy_phase.
        while True:
            try:
                raw = self._read_input(engine)
            except EOFError:
                print(f"\n{dim('Goodbye.')}")
                break
            except KeyboardInterrupt:
                print(f"\n{dim('Goodbye.')}")
                break

            action, payload = classify_input(raw, busy=False, has_pending=False)

            if action in ("noop",):
                continue
            if action == "interrupt":
                print(dim("Nothing running to interrupt."))
                continue
            if action in ("slash", "slash_busy"):
                # A skill exposed as /<slug> runs as a normal turn with the
                # skill loaded; built-in commands take precedence over it.
                skill_payload = self._skill_command_payload(payload)
                if skill_payload is not None:
                    controller.feed(skill_payload)
                    self._run_busy_phase(controller)
                    continue
                if not self._handle_slash_command(payload, engine):
                    break  # /quit — fall through to the save/shutdown path
                continue

            # submit / also / redirect — all start a turn when idle.
            note = paste_summary(payload)
            if note:
                print(note)
            if action == "redirect":
                controller.redirect(payload)
            else:
                controller.feed(payload)
            self._run_busy_phase(controller)

        controller.interrupt()
        controller.wait(timeout=5.0)

        # Generate a brief summary for the next session
        if engine.turn_count >= 1:
            self._save_exit_summary(engine)

        path = save_session(self.session)
        print(dim(f"Session saved: {path}"))
        return 0

    def _smart_compact(self, engine: QueryEngine) -> None:
        """Ask the model to summarize the conversation, save it, then clear context."""
        from ..engine.query_engine import Spinner

        if len(engine.messages) <= 2:
            print(dim("Nothing to compact."))
            return

        # Step 1: Ask model to summarize
        spinner = Spinner("Summarizing conversation...")
        spinner.start()

        try:
            summary_msg, usage = engine.backend.chat_completion(
                messages=engine.messages + [
                    from_dict({"role": "user", "content": COMPACT_SUMMARY_PROMPT})
                ],
                tools=None,  # no tools for summary
                temperature=0.3,
                max_tokens=2048,
            )
            summary = summary_msg.content or "(no summary generated)"
        except Exception as e:
            spinner.stop()
            print(error(f"Failed to generate summary: {e}"))
            return
        finally:
            spinner.stop()

        # Step 2: Save summary to file
        summaries_dir = CONFIG_DIR / "summaries"
        summaries_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        summary_file = summaries_dir / f"{self.session.session_id}_{timestamp}.txt"

        summary_content = (
            f"Session: {self.session.session_id}\n"
            f"Date: {datetime.now().isoformat()}\n"
            f"CWD: {os.getcwd()}\n"
            f"Turns: {engine.turn_count}\n"
            f"Tokens used: {engine.usage.total}\n"
            f"Messages before compact: {len(engine.messages)}\n"
            f"\n--- Summary ---\n\n"
            f"{summary}\n"
        )
        summary_file.write_text(summary_content)

        # Step 3: Clear context but inject summary as memory
        system_msg = engine.messages[0]  # keep system prompt
        engine.messages = [
            system_msg,
            from_dict({
                "role": "user",
                "content": f"[Previous conversation summary — use this as context]\n\n{summary}"
            }),
            from_dict({
                "role": "assistant",
                "content": "Got it. I have the context from our previous conversation. What's next?"
            }),
        ]
        engine.turn_count = 0

        print(f"\n  {GREEN}Compacted{RESET} {dim(f'({len(engine.messages)} messages)')}\n")

    def _save_exit_summary(self, engine: QueryEngine) -> None:
        """Generate a brief session summary for next-session context carryover."""
        from ..engine.query_engine import Spinner
        from ..core.models import Message as from_dict

        if len(engine.messages) <= 2:
            return

        spinner = Spinner("Saving session summary...")
        spinner.start()
        try:
            summary_msg, _ = engine.backend.chat_completion(
                messages=engine.messages + [
                    from_dict(role="user", content=SESSION_EXIT_SUMMARY_PROMPT)
                ],
                tools=None,
                temperature=0.3,
                max_tokens=512,
            )
            summary = (summary_msg.content or "").strip()
            if summary:
                LAST_SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
                LAST_SESSION_FILE.write_text(
                    f"Date: {datetime.now().isoformat()}\n"
                    f"CWD: {os.getcwd()}\n"
                    f"Turns: {engine.turn_count}\n\n"
                    f"{summary}\n"
                )
        except Exception:
            pass  # don't block exit on summary failure
        finally:
            spinner.stop()

    def _handle_memory(self, parts: list[str], engine: QueryEngine) -> None:
        """Handle /memory commands."""
        from ..engine.query_engine import (
            load_memory, save_memory, MEMORY_FILE, MEMORY_UPDATE_PROMPT, Spinner,
        )

        subcommand = parts[1].strip().lower() if len(parts) > 1 else "update"

        if subcommand == "show":
            memory = load_memory()
            if memory:
                print(f"\n  {CYAN}{BOLD}Persistent Memory{RESET} {dim(str(MEMORY_FILE))}\n")
                for line in memory.split("\n"):
                    print(f"  {line}")
                print()
            else:
                print(dim("  No memory yet. Run /memory after a conversation to create it."))
            return

        if subcommand == "clear":
            if MEMORY_FILE.exists():
                MEMORY_FILE.unlink()
                print(success("  Memory cleared."))
            else:
                print(dim("  No memory to clear."))
            return

        # Default: update memory from conversation
        if len(engine.messages) <= 2:
            print(dim("  Not enough conversation to update memory."))
            return

        current_memory = load_memory() or "(empty — first time)"

        spinner = Spinner("Updating memory...")
        spinner.start()
        try:
            prompt = MEMORY_UPDATE_PROMPT.format(current_memory=current_memory)
            result_msg, _ = engine.backend.chat_completion(
                messages=engine.messages + [from_dict({"role": "user", "content": prompt})],
                tools=None,
                temperature=0.3,
                max_tokens=2048,
            )
            new_memory = result_msg.content or ""
        except Exception as e:
            spinner.stop()
            print(error(f"  Failed to update memory: {e}"))
            return
        finally:
            spinner.stop()

        if new_memory.strip():
            path = save_memory(new_memory)
            print(f"\n  {GREEN}Memory updated.{RESET} {dim(str(path))}\n")
            for line in new_memory.strip().split("\n")[:15]:
                print(f"  {DIM}{line}{RESET}")
            if new_memory.strip().count("\n") > 15:
                print(f"  {DIM}... ({new_memory.strip().count(chr(10)) - 15} more lines){RESET}")
            print()
        else:
            print(dim("  Model returned empty memory. Nothing saved."))

    def _run_selfcheck(self, mode: str) -> tuple[bool, Path, str]:
        commands = _default_selfcheck_commands(mode)
        outputs: list[tuple[int, str, str]] = []
        root = _repo_root()

        for command in commands:
            result = subprocess.run(
                command,
                cwd=root,
                capture_output=True,
                text=True,
            )
            outputs.append((result.returncode, result.stdout, result.stderr))
            if result.returncode != 0:
                break

        report = _format_selfcheck_report(mode, commands[:len(outputs)], outputs)
        SELF_CHECK_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        report_path = SELF_CHECK_DIR / f"selfcheck_{timestamp}.txt"
        report_path.write_text(report, encoding="utf-8")
        SELF_CHECK_LATEST.write_text(report, encoding="utf-8")
        self.last_selfcheck_report = report_path
        passed = all(code == 0 for code, _, _ in outputs)
        return passed, report_path, report

    def _remember_selfcheck(self, report: str) -> Path:
        from ..engine.query_engine import load_memory, save_memory

        updated = _upsert_selfcheck_memory(load_memory(), report)
        return save_memory(updated)

    def _skill_command_payload(self, raw: str) -> str | None:
        """If `raw` is `/<slug> [args]` naming a bundle or skill (and not a
        reserved built-in command), return a turn payload that loads it inline
        and asks the model to apply it. Bundles win on slug collision. Else None."""
        if not raw.startswith("/"):
            return None
        head, _, rest = raw[1:].partition(" ")
        slug = head.strip().lower()
        reserved = {c.lstrip("/") for c in SLASH_COMMANDS}
        if not slug or slug in reserved:
            return None
        resolved = self.skill_store.resolve_command(slug)
        if resolved is None:
            return None
        kind, obj = resolved
        args = rest.strip()
        task = args or "Apply this to the current context."

        if kind == "bundle":
            members = self.skill_store.bundle_members(obj)
            if not members:
                return None  # all member skills vanished — fall back to /command
            rendered = [self._substitute_skill_args(self._cap_skill_body(m.body), args)
                        for m in members]
            any_templated = any(used for _, used in rendered)
            sections = "\n\n".join(
                f"## Skill: {m.name}\n{body}"
                for m, (body, _) in zip(members, rendered)
            )
            names = ", ".join(m.name for m in members)
            tail = "" if (any_templated and args) else f"\n\n--- Task ---\n{task}"
            return (
                f"[BUNDLE INVOKED: {obj.name} — {names}]\n"
                f"Apply these skills together for the task below; reconcile them "
                f"if they overlap.\n\n{sections}{tail}"
            )

        # A single skill. Load the full body inline — an explicit /slug
        # invocation means the user wants this procedure, so skip disclosure.
        body, templated = self._substitute_skill_args(self._cap_skill_body(obj.body), args)
        tail = "" if (templated and args) else f"\n\n--- Task ---\n{task}"
        return (
            f"[SKILL INVOKED: {obj.name}]\n"
            f"Follow this skill's procedure for the task below.\n\n"
            f"{body}{tail}"
        )

    @staticmethod
    def _cap_skill_body(body: str, limit: int = 8000) -> str:
        """Inline /slug injection puts the whole body in one turn — cap it so
        a runaway skill (or many-member bundle) can't eat the context window.
        The model can still skill_view the full text on demand."""
        if len(body) <= limit:
            return body
        return (body[:limit]
                + f"\n\n[... skill body truncated at {limit} chars — "
                f"load the rest with skill_view if needed]")

    @staticmethod
    def _substitute_skill_args(body: str, args: str) -> tuple[str, bool]:
        """$ARGUMENTS / $1..$9 templating for skill bodies (Claude Code style).

        $ARGUMENTS receives the whole argument string, $N the Nth
        whitespace-split word (empty when absent). Returns (body, templated);
        when the body has no placeholders the caller appends the classic
        "--- Task ---" section instead."""
        if "$ARGUMENTS" not in body and not re.search(r"\$[1-9]", body):
            return body, False
        words = args.split()
        out = body.replace("$ARGUMENTS", args)
        out = re.sub(
            r"\$([1-9])",
            lambda m: words[int(m.group(1)) - 1] if int(m.group(1)) <= len(words) else "",
            out,
        )
        return out, True

    def _handle_learn(self, parts: list[str], engine: QueryEngine) -> None:
        """/learn <source>  — author a skill from a dir/file/notes/this chat.
        /learn auto [on|off] — toggle automatic recurring-workflow detection."""
        arg = parts[1].strip() if len(parts) > 1 else ""
        cfg = self.config

        # Sub-command: auto-detection toggle (the self-learning detector).
        first = arg.split(maxsplit=1)[0].lower() if arg else ""
        if first == "auto" or not arg:
            mode = arg.split(maxsplit=1)[1].strip().lower() if " " in arg else ""
            if mode in ("on", "enable"):
                cfg.self_learning = True
                engine._detector = None  # rebuild lazily next turn
                cfg.save()
                print(success(
                    "Auto-learning ON — when you repeat a multi-step workflow, "
                    "Tether will suggest saving it as a skill. Toggle with "
                    "/learn auto off."
                ))
            elif mode in ("off", "disable"):
                cfg.self_learning = False
                cfg.save()
                print(dim("Auto-learning OFF — no automatic suggestions."))
            else:
                state = "ON" if getattr(cfg, "self_learning", False) else "OFF"
                print(f"  Auto-learning: {bold(state)}  "
                      f"{dim('(/learn auto on|off)')}")
                print(dim("  Author a skill now: /learn <dir|file|notes>  "
                          "or  /learn this chat"))
            return

        # Otherwise: author a skill from the given source via a guided turn.
        source = arg
        chat = source.lower() in ("this", "chat", "this chat", "conversation", "session")
        if chat:
            instruction = (
                "Author a new reusable skill from what we just did in THIS "
                "conversation. Identify the repeatable procedure, then call "
                "skill_manage(action=\"create\", ...) with a clear name, a "
                "one-line description, a category, and a body containing "
                "'When to Use', 'Procedure', 'Pitfalls', and 'Verification' "
                "sections. Confirm the saved skill name when done."
            )
        else:
            instruction = (
                f"Author a new reusable skill from this source: {source!r}. "
                f"If it's a path, read the relevant files (file_read/glob) to "
                f"understand the procedure; if it's a URL, note you can't fetch "
                f"it and ask the user to paste the content; if it's freeform "
                f"notes, use them directly. Then call skill_manage(action="
                f"\"create\", ...) with a clear name, one-line description, "
                f"category, and a body with 'When to Use', 'Procedure', "
                f"'Pitfalls', and 'Verification' sections. Confirm the saved "
                f"skill name when done."
            )
        if not getattr(self.config, "skills_write_approval", False):
            print(dim("Authoring a skill… (set skills_write_approval to review "
                      "writes before they land)"))
        self._controller.feed(instruction)
        self._run_busy_phase(self._controller)

    def _handle_skills(self, parts: list[str], engine: QueryEngine) -> None:
        """List installed skills, show one, or forget a user skill."""
        store = self.skill_store
        sub = parts[1].split() if len(parts) > 1 else []
        action = sub[0].lower() if sub else ""

        if action == "doctor":
            problems = store.diagnostics()
            if not problems:
                print(success("All skill and bundle files parse cleanly."))
            else:
                print(warning(f"{len(problems)} problem"
                              f"{'s' if len(problems) != 1 else ''} found:"))
                for p in problems:
                    print(f"  {YELLOW}⚠{RESET} {p}")
            return

        if action == "forget" and len(sub) > 1:
            name = sub[1]
            sk = store.get(name)
            if sk is not None and not sk.writable:
                print(warning(f"'{sk.slug}' is a built-in skill and can't be removed."))
            elif store.delete(name):
                print(success(f"Forgot skill '{name}'."))
            else:
                print(warning(f"No skill named '{name}'."))
            return
        if action == "show" and len(sub) > 1:
            resolved = store.resolve_command(sub[1])
            if resolved is None:
                print(warning(f"No skill or bundle named '{sub[1]}'."))
            elif resolved[0] == "bundle":
                b = resolved[1]
                print(f"{bold(b.name)} {dim(f'(bundle, {b.source})')}")
                if b.description:
                    print(f"  {b.description}")
                print(dim("  Loads: ") + ", ".join(b.skills))
            else:
                sk = resolved[1]
                ver = sk.version or "?"
                print(f"{bold(sk.name)} {dim(f'(v{ver}, {sk.source})')}")
                print(sk.body)
            return

        skills = store.available(set(self.tool_registry.names()))
        bundles = store.bundles()
        if not skills and not bundles:
            print(dim("No skills installed. Author one with /learn <source> "
                      "or /learn this chat."))
            return
        print(f"{bold('Skills')} {dim('(use /<name>, or ask in plain language):')}")
        by_cat: dict[str, list] = {}
        for s in sorted(skills, key=lambda s: (s.category, s.slug)):
            by_cat.setdefault(s.category, []).append(s)
        for cat, items in sorted(by_cat.items()):
            print(f"  {dim(cat)}")
            for s in items:
                tag = dim("  built-in") if not s.writable else ""
                hint = f" {dim(s.argument_hint)}" if s.argument_hint else ""
                print(f"    {MAGENTA}/{s.slug}{RESET}{hint}  {s.description}{tag}")
        if bundles:
            print(f"  {dim('bundles')} {dim('(load several skills at once)')}")
            for b in sorted(bundles, key=lambda b: b.slug):
                members = ", ".join(b.skills)
                tag = dim("  built-in") if not b.writable else ""
                print(f"    {MAGENTA}/{b.slug}{RESET}  {b.description} "
                      f"{dim(f'[{members}]')}{tag}")
        # Quietly flag broken files so a typo'd skill doesn't just vanish.
        try:
            problems = store.diagnostics()
        except Exception:
            problems = []
        if problems:
            print(warning(f"  ⚠ {len(problems)} skill file problem"
                          f"{'s' if len(problems) != 1 else ''} — "
                          f"run /skills doctor for details"))
        print(dim("  /skills show <name> · /skills forget <name> · "
                  "/skills doctor · /learn <source>"))

    # -- codebase mental model --------------------------------------------
    def _model_on_edit(self, file_path: str) -> None:
        """Edit-hook: translate an edited file path to a repo-relative posix path
        and refresh that one file in the substrate. Crash-proof (the engine also
        guards the call) so a model hiccup never disturbs an edit."""
        model = self.codebase_model
        if model is None:
            return
        try:
            from pathlib import Path
            p = Path(file_path)
            root = model.repo_root
            abs_p = p if p.is_absolute() else (Path(os.getcwd()) / p)
            try:
                rel = abs_p.resolve().relative_to(Path(root).resolve()).as_posix()
            except ValueError:
                return  # edit outside the repo — not part of this model
            if rel.endswith(".py"):
                model.on_edit(rel)
        except Exception:
            pass

    def _model_build_with_bar(self, model) -> bool:
        """Cold-build the mental model with a live in-place progress bar. Shared by
        the automatic first-run build and the manual /persistence build. Returns
        True on success; on failure prints the error and points at /persistence sync."""
        import time as _time
        print(dim(f"Building mental model for {model.repo_root}"))

        def _bar(done: int, total: int, path: str) -> None:
            width = 26
            frac = done / total if total else 1.0
            filled = int(width * frac)
            bar = "█" * filled + "░" * (width - filled)
            short = path if len(path) <= 32 else "…" + path[-31:]
            sys.stdout.write(
                f"\r  {MAGENTA}⟳{RESET} {MAGENTA}{bar}{RESET} "
                f"{int(frac * 100):3d}%  {DIM}{done}/{total}  {short}{RESET}\x1b[K")
            sys.stdout.flush()

        t0 = _time.time()
        try:
            res = model.build(on_progress=_bar)
        except Exception as e:
            sys.stdout.write("\r\x1b[K")
            print(error(f"Build failed: {e} — you can retry with /persistence sync."))
            return False
        dt = _time.time() - t0
        s = model.store
        sys.stdout.write("\r\x1b[K")  # clear the progress line
        sys.stdout.flush()
        print(success(
            f"✓ Mental model built in {dt:.1f}s — {res.get('files', 0)} files "
            f"({res.get('indexed', 0)} changed) · {s.count_nodes()} symbols · "
            f"{len(s.all_edges())} edges · {len(s.all_modules())} modules"))
        return True

    def _handle_persistence(self, parts: list[str], engine: QueryEngine) -> None:
        """/persistence [status|build|sync|check|ask <q>] — control the persistent
        codebase mental model (the repo's world model, not the LLM — that's /model)."""
        if not getattr(self.config, "codebase_model_enabled", False):
            print(dim("Mental model is off. Set codebase_model_enabled in your "
                      "config, then restart."))
            return
        sub = parts[1].split() if len(parts) > 1 else []
        action = sub[0].lower() if sub else "status"

        if action == "gc":
            # Needs no active model — it sweeps the whole models dir, including
            # orphans for repos that no longer exist and legacy home-dir DBs.
            from ..core.codebase_model.service import gc_models
            report = gc_models()
            for item in report["removed"]:
                mb = item["bytes"] / 1_000_000
                print(success(f"  removed {item['db']} ({mb:.1f} MB) — root: {item['root']}"))
            if report["unknown"]:
                print(dim(f"  {len(report['unknown'])} db(s) predate root tracking — "
                          f"left alone: {', '.join(report['unknown'])}"))
            if not report["removed"]:
                print(dim("  nothing to collect."))
            print(dim(f"  {len(report['kept'])} live model db(s) kept."))
            return

        model = self.codebase_model
        if model is None:
            print(warning("Codebase model failed to initialise this session."))
            return

        if action == "build":
            self._model_build_with_bar(model)
            return
        if action == "sync":
            try:
                res = model.sync()
                print(success(f"Synced: {res.get('indexed', 0)} changed, "
                              f"{res.get('removed', 0)} removed, "
                              f"{res.get('invalidated', 0)} belief(s) invalidated."))
            except Exception as e:
                print(error(f"Sync failed: {e}"))
            return
        if action == "check":
            files = sub[1:] or None
            vios = (model.invariants.check_diff(files) if files
                    else model.invariants.check_all())
            vios += model.invariants.detect_rejected(files or model.store.all_files())
            if not vios:
                print(success("No violations."))
                return
            for v in vios:
                mark = f"{RED}BLOCKING{RESET}" if v.blocking else dim("soft")
                print(f"  [{mark}] {v.claim} {dim('at ' + v.location)}")
            return
        if action == "ask" and len(sub) > 1:
            question = parts[1].split(maxsplit=1)[1]
            print(model.answer(question))
            return

        # status (default)
        s = model.store
        commit = s.get_meta("commit") or "(unbuilt)"
        print(f"{bold('Mental model')} {dim('@ ' + str(model.repo_root))}")
        print(f"  commit {commit} · {s.count_nodes()} symbols · "
              f"{len(s.all_edges())} edges · {len(s.all_files())} files")
        print(f"  {s.count_beliefs()} belief(s) · {len(s.all_invariants())} invariant(s) · "
              f"{len(s.all_decisions())} decision(s)")
        print(dim("  /persistence build · /persistence sync · "
                  "/persistence check [files…] · /persistence ask <q>"))

    def _handle_model(self, parts: list[str], engine: QueryEngine) -> None:
        """/model — the LLM you're running.

          /model stats        show the current model + token usage
          /model local        switch to the local GGUF model
          /model api [name]   switch to an API-key model (auto-picks the provider
                              whose key is set; name one to override)

        A bare /model shows stats. Switching reuses the provider machinery."""
        sub = parts[1].split() if len(parts) > 1 else []
        action = sub[0].lower() if sub else "stats"

        if action in ("stats", "status", "info"):
            self._show_model_stats(engine)
            return
        if action in ("local", "off", "none"):
            self._handle_provider(["/model", "local"], engine)
            return
        if action == "api":
            self._switch_to_api(engine, sub[1].lower() if len(sub) > 1 else "")
            return
        # Power use: a bare provider name still switches directly.
        if action in REMOTE_PROVIDERS:
            self._handle_provider(["/model", action], engine)
            return
        print(warning(f"Unknown option '{action}'. Use: /model stats · "
                      "/model local · /model api"))

    def _show_model_stats(self, engine: QueryEngine) -> None:
        """Show what /model currently reflects: the active model, whether it's
        local or API, key status, context, and running token usage."""
        cfg = self.config
        if cfg.is_codex:
            print(f"  model:    {bold('codex')} · {cfg.api_model}  {dim('(login-based)')}")
        elif cfg.is_remote:
            key_set = cfg.has_api_key()
            print(f"  model:    {bold(cfg.provider)} · {cfg.api_model}  {dim('(API)')}")
            print(f"  api key:  ${cfg.api_key_env}: "
                  f"{success('set') if key_set else warning('NOT SET')}")
            if cfg.reasoning_effort:
                print(f"  thinking: {cfg.reasoning_effort}")
        else:
            model = Path(cfg.model_path).stem if cfg.model_path else "(none)"
            print(f"  model:    {bold('local')} · {model}  {dim('(GGUF)')}")
            print(f"  server:   {cfg.server_url}")
        print(f"  context:  {cfg.context_size:,}")
        u = getattr(engine, "usage", None)
        if u is not None:
            print(f"  tokens:   {u.input_tokens:,} in · {u.output_tokens:,} out "
                  f"· {u.total:,} total")
        print(dim("  change with: /model local · /model api"))

    def _switch_to_api(self, engine: QueryEngine, explicit: str = "") -> None:
        """Switch to an API-key model. With no name, auto-pick the provider whose
        key is actually set (preferring the one already configured); otherwise use
        the named provider. API-key providers are those with an api_key_env."""
        cfg = self.config
        candidates = [n for n, p in REMOTE_PROVIDERS.items() if p.get("api_key_env")]

        if explicit:
            if explicit not in REMOTE_PROVIDERS:
                print(error(f"  unknown API provider: {explicit}"))
                print(dim(f"  known API providers: {', '.join(candidates)}"))
                return
            self._handle_provider(["/model", explicit], engine)
            return

        def key_set(name: str) -> bool:
            return cfg.has_api_key(name)

        if cfg.provider in candidates and key_set(cfg.provider):
            chosen = cfg.provider
        else:
            chosen = next((n for n in candidates if key_set(n)), None)

        if chosen is None:
            envs = ", ".join(f"${REMOTE_PROVIDERS[n]['api_key_env']}" for n in candidates)
            print(warning("  no API key is set for any provider."))
            print(dim(f"  export one of: {envs}   (or: /model api <provider>)"))
            return
        self._handle_provider(["/model", chosen], engine)

    def _handle_provider(self, parts: list[str], engine: QueryEngine) -> None:
        """Show or switch the active model provider in-session.

        Mutates `self.config` in place; the engine's backend reads the same
        config object on each request so the next turn picks up the change.
        """
        target = parts[1].strip().lower() if len(parts) > 1 else ""
        cfg = self.config

        if not target:
            if cfg.is_codex:
                from ..engine.codex_backend import codex_login_status
                status = codex_login_status()
                print(f"  provider:    {bold('codex')} ({cfg.api_model})")
                print(f"  context:     {cfg.context_size:,}")
                print(f"  login:       {status.output if status.ok else warning(status.output)}")
            elif cfg.is_remote:
                print(f"  provider:    {bold(cfg.provider)} ({cfg.api_model})")
                print(f"  context:     {cfg.context_size:,}")
                if cfg.reasoning_effort:
                    print(f"  thinking:    {cfg.reasoning_effort}")
                key_set = cfg.has_api_key()
                print(f"  ${cfg.api_key_env}: {'set' if key_set else warning('NOT SET')}")
            else:
                model = Path(cfg.model_path).stem if cfg.model_path else "(none)"
                print(f"  provider:    {bold('local')}")
                print(f"  model:       {model}")
                print(f"  server:      {cfg.server_url}")
            preset_names = ", ".join(sorted(REMOTE_PROVIDERS))
            print(dim(f"  switch with: /provider {preset_names} | local"))
            return

        if target in ("off", "local", "none"):
            if not cfg.is_remote:
                print(dim("  already on local"))
                return
            backend = InferenceBackend(cfg)
            apply_provider_selection(cfg, "local")
            cfg.save()
            # Verify the local server is up; warn but don't bail — user may
            # start it in another terminal.
            if backend.health_check():
                print(success(f"  switched to local ({cfg.server_url})"))
            else:
                print(warning(f"  switched to local, but no server responding at {cfg.server_url}"))
                print(dim("  start one with: tether serve  (or: tether start, in a new shell)"))
            return

        if target not in REMOTE_PROVIDERS:
            preset_names = ", ".join(sorted(REMOTE_PROVIDERS))
            print(error(f"  unknown provider: {target}"))
            print(dim(f"  known: {preset_names}, local"))
            return

        apply_provider_selection(cfg, target)
        cfg.save()

        msg = f"  switched to {target} ({cfg.api_model})"
        if cfg.reasoning_effort:
            msg += f", thinking={cfg.reasoning_effort}"
        print(success(msg))
        if not cfg.has_api_key():
            print(warning(f"  ${cfg.api_key_env} is not set — requests will fail"))

    def _handle_selfcheck(self, parts: list[str], engine: QueryEngine) -> None:
        subcommand = parts[1].strip().lower() if len(parts) > 1 else "quick"

        if subcommand == "show":
            if self.last_selfcheck_report and self.last_selfcheck_report.exists():
                print(f"  Last selfcheck: {self.last_selfcheck_report}")
            elif SELF_CHECK_LATEST.exists():
                print(f"  Last selfcheck: {SELF_CHECK_LATEST}")
            else:
                print(dim("  No selfcheck report yet."))
            return

        mode = "deep" if subcommand == "deep" else "quick"
        print(dim(f"Running {mode} selfcheck from {_repo_root()}"))
        passed, report_path, report = self._run_selfcheck(mode)
        memory_path = self._remember_selfcheck(report)
        engine.messages.append(from_dict({
            "role": "user",
            "content": _build_selfcheck_context_message(report_path, report),
        }))

        if passed:
            print(success(f"Selfcheck passed: {report_path}"))
        else:
            print(error(f"Selfcheck failed: {report_path}"))

        print(dim(f"Persistent memory updated: {memory_path}"))
        print(dim("Selfcheck result injected into the current conversation context."))

        preview_lines = report.strip().splitlines()[:16]
        print()
        for line in preview_lines:
            print(f"  {DIM}{line}{RESET}")
        if len(report.strip().splitlines()) > len(preview_lines):
            print(f"  {DIM}...{RESET}")
        print()

    def _handle_slash_command(self, cmd: str, engine: QueryEngine) -> bool:
        parts = cmd.split(maxsplit=1)
        command = parts[0].lower()

        if command in ("/help", "/", "/?", "/commands"):
            print(f"""
{bold('Conversation')}
  /plan [on|off]        Think deeper: plan before acting
  /clear                Clear the context completely
  /compact              Summarize, save, then trim context
  /why <file>[:<line>]  The model's reasoning for a line it edited

{bold('Session')}
  /resume [<id>]        List saved conversations / restore one
  /save                 Save this session now (also autosaves every turn)
  /session              Session info · /history events · /summaries past chats
  /usage /context       Token and context-window usage
  /cd <path>            Change working directory

{bold('Skills & memory')}
  /skills               List skills and bundles (invoke any as /<name>)
  /skills show|forget|doctor <name>
  /learn <source>       Author a skill from a dir, file, notes, or "this chat"
  /memory [show|clear]  Persistent memory

{bold('Codebase mental model')}
  /persistence          Status (auto-builds on first run in a repo)
  /persistence build|sync|check|ask <q>|gc

{bold('Model & provider')}
  /model stats|local|api    The LLM you're running / switch it
  /provider [<name>]        Show or switch remote provider

{bold('While a task is running')}
  type anything         Queues as a follow-up for when the task finishes
  /redirect <text>      Stop now, take this new direction instead
  /stop  (or Ctrl-C)    Interrupt the task

{bold('Misc')}
  /selfcheck [deep|show]    Compile/import smoke checks (+ unit tests)
  /quit /exit               Leave Tether
""")
            return True

        if command == "/provider":
            self._handle_provider(parts, engine)
            return True

        if command == "/why":
            self._handle_why(parts)
            return True

        if command == "/plan":
            arg = parts[1].strip().lower() if len(parts) > 1 else ""
            if arg in ("on", "enable"):
                engine.plan_mode = True
            elif arg in ("off", "disable"):
                engine.plan_mode = False
            else:
                engine.plan_mode = not engine.plan_mode
            if engine.plan_mode:
                print(success(
                    "Plan mode ON — Tether will think harder, investigate first, and lay "
                    "out a plan before changing anything. Toggle off with /plan off."
                ))
            else:
                print(dim("Plan mode OFF."))
            return True

        if command == "/learn":
            self._handle_learn(parts, engine)
            return True

        if command == "/skills":
            self._handle_skills(parts, engine)
            return True

        if command == "/persistence":
            self._handle_persistence(parts, engine)
            return True

        if command == "/model":
            self._handle_model(parts, engine)
            return True

        if command == "/clear":
            engine.messages = engine.messages[:1]
            engine.turn_count = 0
            print(success("Context cleared."))
            return True

        if command == "/compact":
            self._smart_compact(engine)
            return True

        if command == "/history":
            print(self.history.as_text() or dim("No history yet."))
            return True

        if command == "/usage":
            print(f"  Input tokens:  {engine.usage.input_tokens}")
            print(f"  Output tokens: {engine.usage.output_tokens}")
            print(f"  Total:         {engine.usage.total}")
            print(f"  Turns:         {engine.turn_count}")
            return True

        if command == "/context":
            estimated = engine._estimate_message_tokens()
            pct = min(100, int((estimated / self.config.context_size) * 100))
            print(f"  Messages:     {len(engine.messages)}")
            print(f"  Est. tokens:  {estimated}")
            print(f"  Context size: {self.config.context_size}")
            print(f"  Usage:        {pct}%")
            if pct > 75:
                print(f"  {YELLOW}Consider running /compact to free up context{RESET}")
            return True

        if command == "/session":
            print(f"  Session ID: {self.session.session_id}")
            print(f"  CWD: {os.getcwd()}")
            print(f"  Messages: {len(engine.messages)}")
            return True

        if command == "/cd":
            target = parts[1] if len(parts) > 1 else os.path.expanduser("~")
            target = os.path.expanduser(target)
            if os.path.isdir(target):
                os.chdir(target)
                bash_tool = self.tool_registry.get("bash")
                if bash_tool:
                    bash_tool._default_cwd = os.getcwd()
                print(success(f"Changed to: {os.getcwd()}"))
            else:
                print(error(f"Not a directory: {target}"))
            return True

        if command == "/save":
            if engine is not None:
                self.session.messages = [m.to_dict() for m in engine.messages]
                self.session.cwd = os.getcwd()
            path = save_session(self.session)
            print(success(f"Session saved: {path}"))
            return True

        if command == "/resume":
            self._handle_resume(parts, engine)
            return True

        if command == "/memory":
            self._handle_memory(parts, engine)
            return True

        if command == "/selfcheck":
            self._handle_selfcheck(parts, engine)
            return True

        if command == "/summaries":
            summaries_dir = CONFIG_DIR / "summaries"
            if not summaries_dir.exists():
                print(dim("No summaries yet."))
                return True
            files = sorted(summaries_dir.glob("*.txt"), key=lambda p: p.stat().st_mtime, reverse=True)
            if not files:
                print(dim("No summaries yet."))
                return True
            for f in files[:10]:
                first_line = f.read_text().split("\n")[0]
                print(f"  {dim(f.stem)}  {first_line}")
            return True

        if command in ("/quit", "/exit"):
            print(f"\n{dim('Goodbye.')}")
            return False

        print(warning(f"Unknown command: {command}"))
        return True

    def _run_busy_phase(self, controller) -> None:
        """Drive a running turn: the engine streams to the terminal directly
        while we poll stdin for type-ahead. Returns when the turn(s) go idle.

        We deliberately do NOT keep a prompt_toolkit prompt up here — repainting
        it for every streamed token is what caused flicker, lag and out-of-order
        output. Type-ahead is read in cooked mode via select(); during this phase
        a pending broker request (tool approval / clarifying question) is fed the
        next typed line instead of being treated as an /also."""
        import select

        # VCS may have moved under us mid-session (pull/checkout in another
        # terminal). One rev-parse when clean; an incremental sync when not.
        if self.codebase_model is not None:
            synced = self.codebase_model.sync_if_commit_changed()
            if synced and (synced.get("indexed") or synced.get("removed")):
                print(dim(f"  mental model resynced: HEAD moved "
                          f"({synced.get('indexed', 0)} reindexed, "
                          f"{synced.get('removed', 0)} removed, "
                          f"{synced.get('invalidated', 0)} beliefs invalidated)"))

        if not self._busy_hint_shown:
            print(dim("  (type to queue an /also follow-up · /redirect <text> to change "
                      "course · /stop to interrupt)"))
            self._busy_hint_shown = True

        # Suppress terminal echo while the engine owns stdout — cooked-mode
        # type-ahead otherwise echoes into the token stream and garbles it.
        # Echo comes back whenever a broker prompt awaits a typed answer
        # (approval / clarifying question); queued lines are echoed back
        # cleanly on their own line after Enter instead.
        saved_attrs = None
        fd = None
        echo_on = True
        try:
            import termios
            fd = sys.stdin.fileno()
            saved_attrs = termios.tcgetattr(fd)
        except Exception:
            termios = None

        def _set_echo(on: bool) -> None:
            nonlocal echo_on
            if saved_attrs is None or echo_on == on:
                return
            try:
                attrs = termios.tcgetattr(fd)
                if on:
                    attrs[3] |= termios.ECHO
                else:
                    attrs[3] &= ~termios.ECHO
                termios.tcsetattr(fd, termios.TCSANOW, attrs)
                echo_on = on
            except Exception:
                pass

        try:
            while controller.is_busy():
                _set_echo(self._broker.has_pending())
                try:
                    ready, _, _ = select.select([sys.stdin], [], [], 0.2)
                    if not ready:
                        continue
                    line = sys.stdin.readline()
                except KeyboardInterrupt:
                    controller.interrupt()
                    self._broker.cancel()
                    print(f"\n{warning('Interrupting…')}")
                    break
                except (OSError, ValueError):
                    # stdin not selectable (not a tty) — just wait the turn out.
                    controller.wait(timeout=0.2)
                    continue

                if line == "":  # EOF / Ctrl-D while busy
                    controller.interrupt()
                    self._broker.cancel()
                    break
                line = line.rstrip("\n")

                if self._broker.has_pending():
                    self._broker.fulfill(line)
                    continue
                if not echo_on and line.strip():
                    print(dim(f"  ↳ {line}"))
                self._dispatch_busy_line(line, controller)
        finally:
            if saved_attrs is not None:
                try:
                    termios.tcsetattr(fd, termios.TCSANOW, saved_attrs)
                except Exception:
                    pass

    def _dispatch_busy_line(self, line: str, controller) -> None:
        action, payload = classify_input(line, busy=True, has_pending=False)
        if action == "noop":
            return
        if action == "interrupt":
            controller.interrupt()
            print(warning("Interrupting…"))
        elif action == "redirect":
            controller.redirect(payload)
            print(f"{MAGENTA}↻ redirecting{RESET}")
        elif action == "also":
            note = paste_summary(payload)
            if note:
                print(note)
            if controller.feed(payload) == "queued":
                print(dim(f"⏳ queued — will run when the current task finishes "
                          f"({len(controller.queued())} pending)"))
        elif action == "slash_busy":
            print(dim("A task is running. Use /also <text>, /redirect <text>, or /stop."))

    def _handle_resume(self, parts: list[str], engine: QueryEngine) -> None:
        """/resume — list saved sessions; /resume <id> — restore one.

        The stored transcript replaces everything after the *current* system
        message (never the stored one — prompts/skills/config may have moved
        on since it was written). The restored session's id is adopted so
        subsequent autosaves continue the same file."""
        arg = parts[1].strip() if len(parts) > 1 else ""
        # Tolerate a pasted filename/path: sessions/<id>.json -> <id>
        if arg.endswith(".json") or "/" in arg:
            arg = Path(arg).stem

        if not arg:
            stored = [s for s in list_sessions()
                      if s.messages and s.session_id != self.session.session_id]
            if not stored:
                print(dim("No resumable sessions found."))
                return
            print(bold("Recent sessions:"))
            for s in stored[:10]:
                first_user = next(
                    (m.get("content") or "" for m in s.messages
                     if m.get("role") == "user"), "")
                preview = " ".join(first_user.split())[:60]
                turns = sum(1 for m in s.messages if m.get("role") == "user")
                print(f"  {CYAN}{s.session_id}{RESET}  "
                      f"{dim(s.cwd or '?')}  {turns} turn{'s' if turns != 1 else ''}")
                if preview:
                    print(f"      {dim(preview)}")
            print(dim("\nResume one with: /resume <id>"))
            return

        stored = load_session(arg)
        if stored is None or not stored.messages:
            print(error(f"No saved conversation for session '{arg}'."))
            return
        try:
            from ..core.models import Message
            restored = [Message.from_dict(d) for d in stored.messages
                        if d.get("role") != "system"]
        except Exception as exc:
            print(error(f"Could not restore session: {exc}"))
            return
        system = engine.messages[0] if engine.messages else None
        engine.messages = ([system] if system else []) + restored
        self.session = stored
        self._last_total = 0
        if stored.cwd and os.path.isdir(stored.cwd) and stored.cwd != os.getcwd():
            print(dim(f"  (session was in {stored.cwd} — /cd there if you want "
                      f"file paths to resolve)"))
        turns = sum(1 for m in restored if m.role == "user")
        print(success(f"Resumed session {stored.session_id}: "
                      f"{len(restored)} messages, {turns} user turns."))

    def _on_turn_complete(self, result, elapsed: float = 0.0) -> None:
        """Bookkeeping + a compact footer after each background turn."""
        eng = self._engine
        self.history.add("turn", f"tools={result.tool_calls_made} stop={result.stop_reason}")
        tok_delta = 0
        if eng is not None:
            self.session.input_tokens = eng.usage.input_tokens
            self.session.output_tokens = eng.usage.output_tokens
            tok_delta = max(0, eng.usage.total - self._last_total)
            self._last_total = eng.usage.total
            # Autosave the conversation itself so a crash/exit loses nothing
            # and /resume can restore it in a later session.
            try:
                self.session.messages = [m.to_dict() for m in eng.messages]
                self.session.cwd = os.getcwd()
                save_session(self.session)
            except Exception:
                pass  # persistence must never break the turn footer

        bits = [f"{elapsed:.1f}s"]
        if tok_delta:
            bits.append(f"{_human_tokens(tok_delta)} tok")
        n_tools = len(result.tool_calls_made)
        if n_tools:
            bits.append(f"{n_tools} tool{'s' if n_tools != 1 else ''}")

        # A hairline rule closes the turn: "─ 4.2s · 1.2k tok · 3 tools ───────"
        label = f"─ {' · '.join(bits)} "
        fill = max(0, term_width() - len(label))
        print(f"{DIM}{label}{'─' * fill}{RESET}")

        if result.stop_reason not in ("completed", "cancelled"):
            print(f"  {warning(result.stop_reason)}")

        low = getattr(eng, "_turn_low_confidence", []) if eng else []
        if low:
            shown = ", ".join(f"{os.path.basename(p)} ({c:.2f})" for p, c in low[:3])
            extra = "" if len(low) <= 3 else f" +{len(low) - 3} more"
            n = len(low)
            print(f"  {YELLOW}⚠ {n} low-confidence edit{'s' if n != 1 else ''}: "
                  f"{shown}{extra} — review with /why{RESET}")

        pattern = getattr(eng, "_suggest_skill", None) if eng else None
        if pattern is not None:
            steps = " → ".join(pattern.tool_sequence) or "(varies)"
            print(f"  {GREEN}✨ You've done this kind of workflow "
                  f"{pattern.count}× ({steps}).{RESET}")
            print(f"  {DIM}   Save it as a reusable skill? Run "
                  f"/learn this chat — or just ask me to 'save this as a skill'.{RESET}")
        print()

    def _approval(self, tool_name: str, arguments: dict):
        """Approval prompt that works during a background turn by collecting the
        answer through the broker. Sub-agents (no interactive marker) fall back
        to the legacy direct-input path."""
        if not is_interactive_turn():
            return request_approval(tool_name, arguments)
        print(f"\n  {YELLOW}{BOLD}Approval required{RESET}")
        if tool_name == "agent":
            print(f"  {DIM}The model wants to launch a sub-agent instead of continuing directly.{RESET}")
        elif tool_name == "bash":
            print(f"  {DIM}The model wants to run a shell command on your machine.{RESET}")
        print(f"  {MAGENTA}{tool_name}{RESET}({format_tool_args(arguments)})")
        print(f"  {DIM}Reply: y, n, a (yes for all {tool_name} this session), or feedback{RESET}")
        line = self._broker.request_line(f"approve {tool_name}?")
        if line is None:
            return False
        resp = line.strip()
        lower = resp.lower()
        if lower in ("a", "all", "yes for all", "yes-for-all", "ya"):
            return "ALL"
        if lower in ("y", "yes"):
            return True
        if lower in ("n", "no", ""):
            return False
        return resp

    def _ask_clarifying(self, questions: list[dict]) -> list[dict]:
        """Broker-backed version of ask_clarifying_questions: collects each
        answer through the main loop so a background turn can ask the user."""
        print(f"\n  {CYAN}{BOLD}A few quick questions before I start:{RESET}")
        answers: list[dict] = []
        for i, q in enumerate(questions, 1):
            text = q.get("question", "")
            options = q.get("options", [])
            print(f"\n  {BOLD}{i}. {text}{RESET}")
            for j, opt in enumerate(options, 1):
                print(f"     {MAGENTA}{j}{RESET}) {opt}")
            print(f"     {DIM}(enter a number, or type your own answer){RESET}")
            line = self._broker.request_line(f"answer {i}/{len(questions)}")
            if line is None:
                raise KeyboardInterrupt  # AskUserTool turns this into a graceful dismissal
            answers.append({"question": text, "answer": resolve_choice(line, options)})
        return answers

    def _handle_why(self, parts) -> None:
        """`/why <file>:<line>` — surface the model's recorded reasoning for a
        line it edited. `/why <file>:<a>-<b>` for a range, `/why <file>` to list
        which lines have rationale."""
        if len(parts) < 2 or not parts[1].strip():
            print(dim("Usage: /why <file>:<line>   (or /why <file> to list annotated lines)"))
            return
        arg = parts[1].strip()
        if ":" in arg:
            path, _, linespec = arg.rpartition(":")
        else:
            path, linespec = arg, ""
        path = os.path.expanduser(path)

        # No line given -> list annotated lines for the file.
        if not linespec:
            entries = self.rationale_store.entries_for(path)
            if not entries:
                print(dim(f"No recorded reasoning for {path}."))
                return
            print(f"  {bold('Annotated lines in ' + path + ':')}")
            for ln in sorted(entries):
                print(f"    {MAGENTA}{ln}{RESET}  {dim(entries[ln]['code'][:70])}")
            print(dim("  Ask /why <file>:<line> for the reasoning."))
            return

        # Parse a single line or an a-b range.
        wanted: list[int] = []
        if "-" in linespec:
            a, _, b = linespec.partition("-")
            if a.isdigit() and b.isdigit() and int(a) <= int(b):
                wanted = list(range(int(a), int(b) + 1))
        elif linespec.isdigit():
            wanted = [int(linespec)]
        if not wanted:
            print(warning(f"Bad line spec: {linespec}. Use /why <file>:<line> or <file>:<a>-<b>."))
            return

        found = False
        for ln in wanted:
            info_ = self.rationale_store.lookup(path, ln)
            if info_ is None:
                continue
            found = True
            print(f"\n  {CYAN}{BOLD}{os.path.basename(path)}:{ln}{RESET}  {dim(info_['code'][:80])}")
            if info_.get("moved"):
                actual = info_.get("actual_line")
                if actual:
                    print(f"  {YELLOW}(this line has since moved to line {actual}){RESET}")
                else:
                    print(f"  {YELLOW}(this exact line is no longer in the file; reasoning is from when it was written){RESET}")
            print(f"  {info_['rationale']}")

        if not found:
            annotated = sorted(self.rationale_store.entries_for(path))
            if annotated:
                shown = ", ".join(map(str, annotated))
                print(dim(f"No reasoning recorded for that line. Annotated lines: {shown}"))
            else:
                print(dim(f"No recorded reasoning for {path}. The model annotates non-obvious lines as it edits."))

    @staticmethod
    def _home_abbrev(path: str) -> str:
        home = os.path.expanduser("~")
        return "~" + path[len(home):] if path.startswith(home) else path

    def _print_welcome(self, time_greeting: str) -> None:
        cwd = self._home_abbrev(os.getcwd())
        ctx_k = self.config.context_size // 1024
        if self.config.is_remote:
            model = self.config.api_model or self.config.provider
        else:
            model = Path(self.config.model_path).stem if self.config.model_path else "local model"

        print(f"  {CYAN}{model}{RESET}{DIM} · {ctx_k}K context · {cwd}{RESET}")
        print()
        print(f"  {GREEN}{BOLD}{time_greeting}.{RESET} {random.choice(GREETINGS)}")
        print(f"  {dim('/help commands · /plan plan first · /resume continue a past chat')}")
        print()

    def _bottom_toolbar(self, engine: QueryEngine) -> HTML:
        cwd = self._home_abbrev(os.getcwd())
        estimated_tokens = engine._estimate_message_tokens()
        max_tokens = self.config.context_size
        pct = min(100, int((estimated_tokens / max_tokens) * 100))
        turns = engine.turn_count
        if self.config.is_remote:
            model = self.config.api_model or self.config.provider
        else:
            model = Path(self.config.model_path).stem if self.config.model_path else "default"
        parts = [
            f"<b>{model}</b>",
            f"ctx {pct}%",
            f"turns {turns}",
            f"{cwd}",
        ]
        if getattr(engine, "plan_mode", False):
            parts.insert(0, "<style fg='#c6a0f6'><b>PLAN</b></style>")
        return HTML("  ·  ".join(parts))

    def _read_input(self, engine: QueryEngine) -> str:
        """Read one submission inside a bordered input box — a double-line frame
        that hugs the input and grows with it (the harness-style input frame),
        with the status bar just beneath it.

        Fully isolated and guarded: completion (the `/` menu), history,
        auto-suggest and the enter/paste key handling are all preserved, and ANY
        prompt_toolkit failure degrades to the plain prompt so typing is never
        impossible. EOF / Ctrl-C propagate exactly as the classic prompt did."""
        try:
            from prompt_toolkit.application import Application
            from prompt_toolkit.buffer import Buffer
            from prompt_toolkit.key_binding import KeyBindings
            from prompt_toolkit.layout import (Float, FloatContainer, HSplit,
                                               Layout, VSplit, Window)
            from prompt_toolkit.layout.controls import (BufferControl,
                                                        FormattedTextControl)
            from prompt_toolkit.layout.dimension import Dimension
            from prompt_toolkit.layout.menus import CompletionsMenu
            from prompt_toolkit.layout.processors import BeforeInput

            buf = Buffer(
                completer=TetherCompleter(skill_store=self.skill_store),
                complete_while_typing=True,
                history=self._history,
                auto_suggest=AutoSuggestFromHistory(),
                multiline=True,
            )

            kb = KeyBindings()

            @kb.add("enter")
            def _enter(event):
                # Paste in flight (bytes queued behind Enter) -> literal newline;
                # otherwise submit. Mirrors the classic prompt's behavior.
                if event.app.key_processor.input_queue:
                    buf.insert_text("\n")
                else:
                    buf.append_to_history()  # app.exit() skips validate_and_handle()
                    event.app.exit(result=buf.text)

            @kb.add("escape", "enter")
            def _alt_enter(event):
                buf.insert_text("\n")

            @kb.add("c-j")
            def _ctrl_j(event):
                buf.insert_text("\n")

            @kb.add("/")
            def _slash(event):
                buf.insert_text("/")
                if buf.document.text_before_cursor.lstrip() == "/":
                    try:
                        buf.start_completion(select_first=False)
                    except Exception:
                        pass

            @kb.add("c-c")
            def _ctrl_c(event):
                event.app.exit(exception=KeyboardInterrupt)

            @kb.add("c-d")
            def _ctrl_d(event):
                if not buf.text:
                    event.app.exit(exception=EOFError)

            border = "class:frame.border"
            input_window = Window(
                BufferControl(
                    buffer=buf,
                    input_processors=[BeforeInput(ANSI(self._prompt(engine)))],
                ),
                height=Dimension(min=1),
                wrap_lines=True,
            )

            def hbar():
                return Window(char="─", height=1, style=border)

            def corner(ch):
                return Window(FormattedTextControl(ch), width=1, height=1, style=border)

            def vbar():
                return Window(char="│", width=1, style=border)

            # A rounded thin box that grows with the input; status bar beneath.
            box = HSplit([
                VSplit([corner("╭"), hbar(), corner("╮")]),
                VSplit([vbar(), input_window, vbar()]),
                VSplit([corner("╰"), hbar(), corner("╯")]),
            ])
            status = Window(
                FormattedTextControl(lambda: self._bottom_toolbar(engine)),
                height=1, style="class:bottom-toolbar",
            )
            root = FloatContainer(
                content=HSplit([box, status]),
                floats=[Float(xcursor=True, ycursor=True,
                              content=CompletionsMenu(max_height=12, scroll_offset=1))],
            )
            app = Application(
                layout=Layout(root, focused_element=input_window),
                key_bindings=kb,
                style=self._toolbar_style,
                full_screen=False,
                mouse_support=False,
            )
            result = app.run()
            return result if result is not None else ""
        except (EOFError, KeyboardInterrupt):
            raise
        except Exception:
            # Never block input: fall back to the classic single-line prompt.
            return self._prompt_session.prompt(
                ANSI(self._prompt(engine)),
                bottom_toolbar=self._bottom_toolbar(engine),
            )

    def _prompt(self, engine: QueryEngine) -> str:
        ctx = context_indicator(engine, self.config)
        cwd_name = os.path.basename(os.getcwd()) or os.getcwd()
        plan = f" {MAGENTA}{BOLD}[PLAN]{RESET}" if getattr(engine, "plan_mode", False) else ""
        return f"{BLUE}{BOLD}{cwd_name}{RESET}{ctx}{plan} {CYAN}{BOLD}>{RESET} "


def from_dict(d: dict):
    """Quick helper to build a Message from a dict."""
    from ..core.models import Message
    return Message(role=d["role"], content=d.get("content"))
