from dataclasses import replace
import json

import pytest

from tether.runtime.contracts import Budget, State, TaskSpec, decode_plan, encode_plan, validate_transition


def task(name="edit", **kwargs):
    return TaskSpec(name, "run-1", "Repair parser", "a" * 40, Budget(10000, 60, 1.0, 2),
                    ["Parser tests pass"], ["parser.py"], **kwargs)


def test_wire_roundtrip_keeps_model_independent_dag():
    plan = [task(), task("verify", dependencies=["edit"], role="verifier",
                         verification_commands=[["python", "-m", "pytest", "tests/test_parser.py"]])]
    assert decode_plan(encode_plan("run-1", plan)) == ("run-1", plan)


@pytest.mark.parametrize("tasks", [
    [task(), task()], [task(dependencies=["missing"])],
    [task(dependencies=["verify"]), task("verify", dependencies=["edit"])],
    [replace(task(), run_id="different")],
])
def test_invalid_dags_rejected(tasks):
    with pytest.raises(ValueError):
        encode_plan("run-1", tasks)


@pytest.mark.parametrize("kwargs", [
    {"tokens": 0}, {"tokens": True}, {"seconds": float("inf")},
    {"cost_usd": -1}, {"cost_usd": float("nan")}, {"max_attempts": 0},
])
def test_budgets_are_finite_and_bounded(kwargs):
    with pytest.raises(ValueError):
        Budget(**(dict(tokens=100, seconds=10, cost_usd=0, max_attempts=1) | kwargs))


@pytest.mark.parametrize("path", ["../outside", "/outside", "a/../../b", "a\\b", "x\x00y"])
def test_paths_are_workspace_relative(path):
    with pytest.raises(ValueError):
        replace(task(), impacted_files=[path])


def test_forward_versions_unknown_fields_and_duplicate_keys_fail_closed():
    wire = json.loads(encode_plan("run-1", [task()]))
    for bad in [dict(wire, version=2), dict(wire, version=True), dict(wire, secret="no")]:
        with pytest.raises(ValueError):
            decode_plan(json.dumps(bad))
    with pytest.raises(ValueError, match="Duplicate"):
        decode_plan('{"version":1,"version":2}')
    wire["tasks"][0]["unrecognized"] = True
    with pytest.raises(ValueError):
        decode_plan(json.dumps(wire))


def test_verification_and_terminal_transition_gates():
    for before, after in zip(list(State)[:5], list(State)[1:6]):
        validate_transition(before, after, verified=True)
    with pytest.raises(ValueError, match="evidence"):
        validate_transition(State.VERIFYING, State.COMPLETED)
    with pytest.raises(ValueError, match="exhausted"):
        validate_transition(State.VERIFYING, State.RUNNING)
    validate_transition(State.VERIFYING, State.RUNNING, retries_remaining=1)
    for terminal in [State.COMPLETED, State.FAILED, State.CANCELLED]:
        for state in State:
            with pytest.raises(ValueError):
                validate_transition(terminal, state)


def test_command_text_cannot_replace_argv():
    with pytest.raises(ValueError, match="argv"):
        task(verification_commands=["python -m pytest; rm -rf x"])
