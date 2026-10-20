"""Protocol v1 values shared by the Python baseline and future native runtime.

These are validated wire contracts, not an autonomous scheduler. Paths are
workspace-relative; command arguments are arrays and never shell command text.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
import json
import math
from pathlib import PurePosixPath
import re

PROTOCOL_VERSION = 1
MAX_TASKS = 256
MAX_MESSAGE_BYTES = 1024 * 1024


class State(str, Enum):
    CREATED = "CREATED"
    SCOPED = "SCOPED"
    PLANNED = "PLANNED"
    RUNNING = "RUNNING"
    VERIFYING = "VERIFYING"
    COMPLETED = "COMPLETED"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    PAUSED = "PAUSED"


TRANSITIONS = {
    State.CREATED: {State.SCOPED}, State.SCOPED: {State.PLANNED},
    State.PLANNED: {State.RUNNING}, State.RUNNING: {State.VERIFYING},
    State.VERIFYING: {State.COMPLETED, State.RUNNING},
    State.PAUSED: {State.SCOPED, State.PLANNED, State.RUNNING, State.VERIFYING},
    State.BLOCKED: {State.SCOPED, State.PLANNED, State.RUNNING, State.VERIFYING},
}
TERMINAL = {State.COMPLETED, State.FAILED, State.CANCELLED}


def validate_transition(before, after, *, verified=False, retries_remaining=0):
    before, after = State(before), State(after)
    allowed = set(TRANSITIONS.get(before, ()))
    if before not in TERMINAL:
        allowed.update({State.BLOCKED, State.FAILED, State.CANCELLED, State.PAUSED})
    if before == after or after not in allowed:
        raise ValueError(f"Invalid state transition: {before.value} -> {after.value}")
    if after == State.COMPLETED and not verified:
        raise ValueError("Completion requires executed verification evidence")
    if before == State.VERIFYING and after == State.RUNNING and retries_remaining <= 0:
        raise ValueError("Verification repair budget exhausted")


def _identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", value):
        raise ValueError("Invalid protocol identifier")


def _relative_path(value):
    if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
        raise ValueError("Expected a workspace-relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or len(value) > 4096:
        raise ValueError("Path escapes workspace or exceeds length limit")


@dataclass(frozen=True)
class Budget:
    tokens: int
    seconds: float
    cost_usd: float
    max_attempts: int = 1

    def __post_init__(self):
        if type(self.tokens) is not int or self.tokens <= 0:
            raise ValueError("Token budget must be a positive integer")
        if type(self.max_attempts) is not int or not 1 <= self.max_attempts <= 100:
            raise ValueError("Attempts must be in 1..100")
        for value in (self.seconds, self.cost_usd):
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                raise ValueError("Budgets must be finite and nonnegative")
        if self.seconds == 0:
            raise ValueError("Time budget must be positive")


@dataclass(frozen=True)
class TaskSpec:
    id: str
    run_id: str
    objective: str
    base_revision: str
    budget: Budget
    acceptance: list[str]
    artifacts: list[str]
    dependencies: list[str] = field(default_factory=list)
    impacted_files: list[str] = field(default_factory=list)
    symbols: list[str] = field(default_factory=list)
    verification_commands: list[list[str]] = field(default_factory=list)
    role: str = "implementer"

    def __post_init__(self):
        _identifier(self.id)
        _identifier(self.run_id)
        if not isinstance(self.budget, Budget):
            raise ValueError("Missing task budget")
        if not isinstance(self.objective, str) or not self.objective.strip() or len(self.objective) > 16000:
            raise ValueError("Expected a bounded nonempty objective")
        if not isinstance(self.base_revision, str) or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", self.base_revision):
            raise ValueError("Base revision must be a full Git object ID")
        if self.role not in {"explorer", "planner", "implementer", "verifier", "integrator"}:
            raise ValueError("Unknown worker role")
        for values in (self.acceptance, self.artifacts, self.dependencies, self.impacted_files, self.symbols):
            if not isinstance(values, list) or len(values) > 1024 or any(not isinstance(x, str) or not x or len(x) > 4096 for x in values):
                raise ValueError("Expected bounded lists of nonempty strings")
        if not self.acceptance:
            raise ValueError("Acceptance criteria are required")
        for path in self.artifacts + self.impacted_files:
            _relative_path(path)
        for dependency in self.dependencies:
            _identifier(dependency)
        if self.id in self.dependencies or len(set(self.dependencies)) != len(self.dependencies):
            raise ValueError("Self or duplicate dependency")
        if not isinstance(self.verification_commands, list) or len(self.verification_commands) > 64:
            raise ValueError("Too many verification commands")
        for command in self.verification_commands:
            if (not isinstance(command, list) or not 1 <= len(command) <= 128 or
                    any(not isinstance(arg, str) or "\x00" in arg or len(arg) > 4096 for arg in command) or not command[0]):
                raise ValueError("Verification commands must be bounded argv arrays")


def encode_plan(run_id: str, tasks: list[TaskSpec]) -> str:
    _validate_plan(run_id, tasks)
    text = json.dumps({"version": PROTOCOL_VERSION, "type": "plan", "run_id": run_id,
                       "tasks": [asdict(task) for task in tasks]}, allow_nan=False, separators=(",", ":"))
    if len(text.encode("utf-8")) > MAX_MESSAGE_BYTES:
        raise ValueError("Protocol message exceeds byte limit")
    return text


def decode_plan(text: str) -> tuple[str, list[TaskSpec]]:
    if len(text.encode("utf-8")) > MAX_MESSAGE_BYTES:
        raise ValueError("Protocol message exceeds byte limit")
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON field")
            result[key] = value
        return result
    data = json.loads(text, object_pairs_hook=unique)
    if not isinstance(data, dict) or set(data) != {"version", "type", "run_id", "tasks"}:
        raise ValueError("Invalid protocol envelope")
    if type(data["version"]) is not int or data["version"] != PROTOCOL_VERSION or data["type"] != "plan":
        raise ValueError("Unsupported runtime protocol version or message type")
    if not isinstance(data["tasks"], list) or not 1 <= len(data["tasks"]) <= MAX_TASKS:
        raise ValueError("Task count exceeds protocol bounds")
    try:
        tasks = [TaskSpec(**{**task, "budget": Budget(**task["budget"])}) for task in data["tasks"]]
    except (TypeError, KeyError) as exc:
        raise ValueError("Invalid task schema") from exc
    _validate_plan(data["run_id"], tasks)
    return data["run_id"], tasks


def _validate_plan(run_id, tasks):
    _identifier(run_id)
    if not isinstance(tasks, list) or not 1 <= len(tasks) <= MAX_TASKS:
        raise ValueError("Task count exceeds protocol bounds")
    if any(not isinstance(task, TaskSpec) for task in tasks):
        raise ValueError("Expected task records")
    for task in tasks:
        task.__post_init__()
    nodes = {task.id: task for task in tasks}
    if len(nodes) != len(tasks) or any(task.run_id != run_id for task in tasks):
        raise ValueError("Duplicate task ID or mismatched parent run")
    for task in tasks:
        if not set(task.dependencies) <= nodes.keys():
            raise ValueError("Unknown dependency")
    done = set()
    for _ in range(len(tasks)):
        ready = {task.id for task in tasks if set(task.dependencies) <= done}
        if ready <= done:
            break
        done |= ready
    if len(done) != len(tasks):
        raise ValueError("Cyclic task dependency")
