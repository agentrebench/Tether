from concurrent.futures import ThreadPoolExecutor
import json
import threading

import pytest

from tether.core.models import Message, ToolCall
from tether.runtime.tracing import RunTrace
from .test_cancel import RecordingTool, _engine_with_tools, _quiet


def records(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def engine_trace(tmp_path, monkeypatch):
    engine = _engine_with_tools(RecordingTool("probe"))
    engine.config.runtime_trace_dir = str(tmp_path)
    monkeypatch.setattr(engine, "_learn_from_turn", lambda *args: None, raising=False)
    responses = iter([
        (Message("assistant", tool_calls=[ToolCall("call1", "probe", {"secret": "DO_NOT_RECORD"})]),
         {"prompt_tokens": 7, "completion_tokens": 3}),
        (Message("assistant", "PRIVATE_RESPONSE"), {"prompt_tokens": 10, "completion_tokens": 4}),
    ])
    monkeypatch.setattr(engine.backend, "chat_completion_stream_parsed", lambda **kw: next(responses))
    return engine


def test_trace_captures_actual_calls_without_prompts_arguments_or_results(tmp_path, monkeypatch):
    engine = engine_trace(tmp_path, monkeypatch)
    with _quiet():
        result = engine.submit("PRIVATE_PROMPT")
    path = next(tmp_path.glob("*.jsonl"))
    events = records(path)
    assert result.output == "PRIVATE_RESPONSE"
    assert [event["event"] for event in events] == ["run.started", "model.finished", "tool.finished", "model.finished", "run.finished"]
    assert events[-1]["input_tokens"] == 17 and events[-1]["output_tokens"] == 7
    assert "COMPLETED" not in path.read_text()  # a text return is not verification
    assert not any(secret in path.read_text() for secret in ("PRIVATE_PROMPT", "PRIVATE_RESPONSE", "DO_NOT_RECORD"))
    assert path.stat().st_mode & 0o777 == 0o600


def test_disabled_and_failed_tracing_preserve_behavior(tmp_path, monkeypatch):
    for directory in ["", str(tmp_path / "not-a-directory" / "child")]:
        (tmp_path / "not-a-directory").write_text("occupied")
        engine = engine_trace(tmp_path, monkeypatch)
        engine.config.runtime_trace_dir = directory
        with _quiet():
            result = engine.submit("question")
        assert result.output == "PRIVATE_RESPONSE"
    assert list(tmp_path.glob("*.jsonl")) == []


def test_cancelled_submit_has_terminal_event_without_model_request(tmp_path, monkeypatch):
    engine = engine_trace(tmp_path, monkeypatch)
    cancel = threading.Event()
    cancel.set()
    with _quiet():
        engine.submit("stop", cancel_event=cancel)
    events = records(next(tmp_path.glob("*.jsonl")))
    assert [e["event"] for e in events] == ["run.started", "run.finished"]
    assert events[-1]["stop_reason"] == "cancelled"


def test_raised_submit_is_recorded_and_rethrown(tmp_path, monkeypatch):
    engine = engine_trace(tmp_path, monkeypatch)
    def fail(*args, **kwargs):
        raise RuntimeError("private failure text")
    monkeypatch.setattr(engine, "_submit", fail)
    with pytest.raises(RuntimeError):
        engine.submit("question")
    event = records(next(tmp_path.glob("*.jsonl")))[-1]
    assert event["status"] == "raised" and event["stop_reason"] == "exception"
    assert "private failure" not in str(event)
    assert engine._runtime_trace is None


def test_parallel_events_are_ordered_and_bounded_with_final_event_reserved(tmp_path, monkeypatch):
    monkeypatch.setattr(RunTrace, "MAX_EVENTS", 10)
    trace = RunTrace(tmp_path, "fixture", "fixture")
    with ThreadPoolExecutor(max_workers=4) as workers:
        list(workers.map(lambda i: trace.emit("test", index=i), range(100)))
    trace.finish("returned", "end_turn", (0, 0))
    events = records(trace.path)
    assert len(events) == 10
    assert [e["sequence"] for e in events] == list(range(1, 11))
    assert events[-1]["event"] == "run.finished" and events[-1]["dropped_events"] == 92


def test_trace_retention_stops_without_deleting_existing_files(tmp_path, monkeypatch):
    monkeypatch.setattr(RunTrace, "MAX_FILES", 1)
    trace = RunTrace(tmp_path, "fixture", "fixture")
    trace.finish("returned", "end_turn", (0, 0))
    with pytest.raises(ValueError):
        RunTrace(tmp_path, "fixture", "fixture")
    assert len(list(tmp_path.glob("*.jsonl"))) == 1
