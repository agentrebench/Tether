"""Native terminal boundary: safe settings, actual PTYs and content-free phases."""
from __future__ import annotations

import argparse
import io
import json
import os
from pathlib import Path
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tether import native_app
from tether.cli import build_parser
from tether.core.config import TetherConfig
from tether.core.models import StreamEvent
from tether.engine.codex_backend import CancelledByUser
from tether.engine.query_engine import QueryEngine
from tether.ui.native_activity import NativeActivity
from tether.ui.repl import TetherREPL


def phases(messages):
    return [item.decode("ascii").split(";")[-1].removesuffix("\x07") for item in messages]


def test_native_activity_actual_phases_hide_all_private_content():
    messages = []
    activity = NativeActivity(enabled=True, write=messages.append)
    for kind in ("model_started", "thinking", "thinking", "text", "model_finished"):
        activity.stream(StreamEvent(type=kind, text="PRIVATE_REASONING_AND_SECRET"))
    activity.idle()
    assert phases(messages) == ["thinking", "reasoning", "working", "idle"]
    assert b"PRIVATE" not in b"".join(messages)
    assert messages[0] == b"\x1b]777;tether;1;1;thinking\x07"
    assert all(len(item) < 64 for item in messages)


def test_native_activity_waits_and_stale_subagents_cannot_restart_it():
    messages = []
    activity = NativeActivity(enabled=True, write=messages.append)
    activity.stream(StreamEvent(type="model_started"))
    activity.stream(StreamEvent(type="model_finished"))
    agent = dict(agent_id="a", revision=2, phase="thinking", status="running")
    activity.agent(agent)
    with activity.waiting():
        with activity.waiting():
            activity.agent(dict(agent, revision=3, phase="working"))
    activity.agent(dict(agent, revision=2))
    assert phases(messages) == ["thinking", "working", "thinking", "waiting", "working"]
    activity.idle()
    activity.agent(dict(agent, revision=4))
    activity.stream(StreamEvent(type="thinking"))
    assert phases(messages)[-1] == "idle"


def test_activity_disabled_and_broken_pipe_never_interrupt_agent(monkeypatch):
    messages = []
    monkeypatch.delenv("TETHER_NATIVE_ACTIVITY", raising=False)
    activity = NativeActivity(write=messages.append)
    activity.stream(StreamEvent(type="model_started"))
    with activity.waiting():
        activity.idle()
    assert messages == []
    activity = NativeActivity(enabled=True, write=Mock(side_effect=BrokenPipeError))
    activity.stream(StreamEvent(type="model_started"))
    assert not activity.enabled


@pytest.mark.parametrize("failure", [None, RuntimeError("bad provider"), CancelledByUser("cancelled")])
def test_model_request_always_finishes_activity(failure):
    messages = []
    engine = QueryEngine.__new__(QueryEngine)
    engine._runtime_trace = None
    engine.on_stream_event = messages.append
    engine.backend = SimpleNamespace(chat_completion_stream_parsed=Mock(return_value=("reply", {}), side_effect=failure))
    if failure:
        with pytest.raises(type(failure)):
            engine._model_completion(messages=[])
    else:
        assert engine._model_completion(messages=[]) == ("reply", {})
    assert [event.type for event in messages] == ["model_started", "model_finished"]
    assert all(not event.text for event in messages)


def test_repl_question_and_approval_waits_restore_on_exception():
    repl = TetherREPL.__new__(TetherREPL)
    messages = []
    repl._native_activity = NativeActivity(enabled=True, write=messages.append)
    repl._on_stream_event(StreamEvent(type="model_started"))
    repl._approval_input = Mock(side_effect=RuntimeError("test"))
    with pytest.raises(RuntimeError):
        repl._approval("bash", {})
    repl._ask_clarifying_input = Mock(return_value=[])
    assert repl._ask_clarifying([]) == []
    repl._dispatch_busy_line("/stop", SimpleNamespace(interrupt=lambda: True))
    assert phases(messages) == ["thinking", "waiting", "thinking", "waiting", "thinking", "idle"]


@pytest.mark.parametrize("data", [
    b'{}', b'[]', b'{"shell": "zsh", "theme": "inherit", "font": 1, "theme_file": ""}',
    b'{"shell": "zsh", "theme": "inherit", "font": "ascii", "theme_file": "", "api_key":"secret"}',
    b'{"shell":"zsh", "shell":"bash", "theme":"inherit", "font":"ascii", "theme_file":""}',
    b' ' * 16385, b'not json',
])
def test_settings_reject_unknown_wrong_and_oversized_input(data):
    with pytest.raises((ValueError, TypeError)):
        native_app.read_settings(io.BytesIO(data))


def test_settings_read_one_line_without_waiting_for_eof():
    class SingleLine:
        def readline(self, limit):
            assert limit == 16385
            return b'{"shell":"zsh","theme":"minimal","font":"ascii","theme_file":""}\n'
    assert native_app.read_settings(SingleLine())["theme"] == "minimal"


def test_settings_are_secret_free_and_save_uses_existing_atomic_path(monkeypatch, capsys):
    config = TetherConfig(api_key="PRIVATE", terminal_shell="zsh")
    monkeypatch.setattr(native_app.TetherConfig, "load", lambda: config)
    monkeypatch.setattr(native_app, "theme_catalog", lambda: [SimpleNamespace(id="minimal", name="Minimal", source="Built in")])
    calls = []
    def save(cfg, shell, theme, font, theme_file):
        calls.append((shell, theme, font, theme_file))
        cfg.terminal_theme = theme
    monkeypatch.setattr(native_app, "save_settings", save)
    monkeypatch.setattr(sys, "stdin", io.StringIO('{"shell":"zsh","theme":"minimal","font":"ascii","theme_file":""}\n'))
    assert native_app.run_native_app(argparse.Namespace(action="save-settings", theme=None, cwd=None)) == 0
    output = capsys.readouterr().out
    assert calls == [("zsh", "minimal", "ascii", "")]
    assert "PRIVATE" not in output and "api_key" not in output
    assert json.loads(output)["theme"] == "minimal"
    assert set(json.loads(output)) == {"protocol", "shell", "theme", "font", "theme_file", "home", "themes"}


def test_settings_error_does_not_echo_bad_json(monkeypatch, capsys):
    monkeypatch.setattr(native_app.TetherConfig, "load", TetherConfig)
    monkeypatch.setattr(sys, "stdin", io.StringIO('{"secret":PRIVATE_SECRET}'))
    assert native_app.run_native_app(argparse.Namespace(action="save-settings", theme=None, cwd=None)) == 2
    output = capsys.readouterr().out
    assert "PRIVATE_SECRET" not in output
    assert json.loads(output)["error"] == "Invalid settings JSON."


@pytest.mark.parametrize("action", ["settings", "save-settings", "shell", "agent"])
def test_native_command_parser(action):
    args = build_parser().parse_args(["native-app", action, "--cwd", "/tmp", "--theme", "minimal"])
    assert args.action == action and args.cwd == "/tmp" and args.theme == "minimal"


def _read_until(master, needle, deadline=10):
    output = bytearray()
    stop = time.monotonic() + deadline
    while time.monotonic() < stop:
        if select.select([master], [], [], 0.1)[0]:
            try:
                output.extend(os.read(master, 65536))
            except OSError:
                break
            if needle in output:
                return bytes(output)
    pytest.fail(f"PTY did not produce {needle!r}: {bytes(output)[-1000:]!r}")


@pytest.mark.skipif(sys.platform != "darwin", reason="Native macOS PTY contract")
@pytest.mark.parametrize("shutdown", ["exit", "hangup"])
def test_native_themed_shell_preserves_startup_and_cleans_resources(tmp_path, shutdown):
    home = tmp_path / "home"
    home.mkdir()
    temp = tmp_path / "private"
    temp.mkdir()
    rc = home / ".zshrc"
    rc.write_text("export NATIVE_STARTUP_CHECK=preserved\n")
    master, slave = os.openpty()
    env = dict(os.environ, HOME=str(home), ZDOTDIR=str(home), TMPDIR=str(temp),
               TETHER_CONFIG_DIR=str(tmp_path / "config"), TERM="xterm-256color",
               PYTHONPATH=str(Path(__file__).resolve().parents[2]))
    child = subprocess.Popen([sys.executable, "-m", "tether", "native-app", "shell", "--theme", "minimal", "--cwd", str(home)],
                             stdin=slave, stdout=slave, stderr=slave, env=env, start_new_session=True)
    os.close(slave)
    try:
        _read_until(master, b"\x1b[?2004h")
        os.write(master, b"printf '\\nNATIVE-READY:%s:%s\\n' \"$NATIVE_STARTUP_CHECK\" \"$ZDOTDIR\"\n")
        output = _read_until(master, b"NATIVE-READY:preserved:")
        assert str(home).encode() in output
        resources = list(temp.glob("tether-zsh-*"))
        assert resources
        # Ctrl+C goes to the foreground job, not to the resource-owning wrapper.
        if b"\x1b[?2004h" not in output.split(b"NATIVE-READY:preserved:")[-1]:
            _read_until(master, b"\x1b[?2004h")
        shell_group = os.tcgetpgrp(master)
        os.write(master, b"sleep 30\n")
        deadline = time.monotonic() + 5
        while os.tcgetpgrp(master) == shell_group and time.monotonic() < deadline:
            if select.select([master], [], [], 0.01)[0]:
                os.read(master, 65536)
        assert os.tcgetpgrp(master) != shell_group
        os.write(master, b"\x03")
        _read_until(master, b"\x1b[?2004h")
        os.write(master, b"printf '\\nAFTER-INT:%s\\n' $?\n")
        output = _read_until(master, b"AFTER-INT:130")
        if b"\x1b[?2004h" not in output.split(b"AFTER-INT:130")[-1]:
            _read_until(master, b"\x1b[?2004h")
        background_pid = None
        if shutdown == "exit":
            os.write(master, b"exit\n")
        else:
            os.write(master, b"sleep 30 & print -r -- $! > job.pid; printf '\\nJOB-CREATED\\n'\n")
            output = _read_until(master, b"\r\nJOB-CREATED\r\n")
            background_pid = int((home / "job.pid").read_text())
            if b"\x1b[?2004h" not in output.split(b"\r\nJOB-CREATED\r\n")[-1]:
                _read_until(master, b"\x1b[?2004h")
            os.kill(child.pid, signal.SIGHUP)
        deadline = time.monotonic() + 5
        while child.poll() is None and time.monotonic() < deadline:
            if select.select([master], [], [], 0.05)[0]:
                try:
                    os.read(master, 65536)
                except OSError:
                    break
        code = child.wait(timeout=2)
        assert (code == 0 if shutdown == "exit" else code >= 0), code
        assert all(not path.exists() for path in resources)
        if background_pid is not None:
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                try:
                    os.kill(background_pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.01)
            else:
                pytest.fail("Shell background job survived the native tab hangup")
        assert rc.read_text() == "export NATIVE_STARTUP_CHECK=preserved\n"
    finally:
        if child.poll() is None:
            os.kill(child.pid, signal.SIGHUP)
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)
        os.close(master)


def test_native_save_settings_roundtrip_without_closing_input(tmp_path):
    env = dict(os.environ, TETHER_CONFIG_DIR=str(tmp_path / "config"),
               PYTHONPATH=str(Path(__file__).resolve().parents[2]))
    values = dict(shell="zsh", theme="minimal", font="ascii", theme_file="")
    child = subprocess.Popen([sys.executable, "-m", "tether", "native-app", "save-settings"],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    try:
        child.stdin.write((json.dumps(values) + "\n").encode())
        child.stdin.flush()
        assert select.select([child.stdout], [], [], 5)[0], "Adapter waited for EOF"
        reply = json.loads(child.stdout.readline())
        assert child.wait(timeout=5) == 0
        assert {key: reply[key] for key in values} == values
        loaded = subprocess.run([sys.executable, "-m", "tether", "native-app", "settings"],
                                env=env, capture_output=True, timeout=5, check=True)
        assert {key: json.loads(loaded.stdout)[key] for key in values} == values
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)
        child.stdin.close()
        child.stdout.close()
        child.stderr.close()


@pytest.mark.parametrize("action", ["sessions", "end-session"])
def test_native_session_parser(action):
    args = build_parser().parse_args(["native-app", action, "--tmux", "s-1_x"])
    assert args.action == action and args.session == "s-1_x"
    assert build_parser().parse_args(["native-app", "shell"]).session is None


@pytest.mark.parametrize("name", ["", "a.b", "a:b", "a b", "x" * 33, None, 7])
def test_session_names_reject_target_syntax(name):
    with pytest.raises(ValueError):
        native_app.validate_session(name)


def test_sessions_listing_without_tmux_is_empty_and_filters_foreign_names(monkeypatch):
    monkeypatch.setattr(native_app.shutil, "which", lambda _: None)
    assert native_app.list_sessions() == {"protocol": 1, "tmux": False, "sessions": []}
    with pytest.raises(ValueError):
        native_app.end_session("gone")
    monkeypatch.setattr(native_app.shutil, "which", lambda _: "/usr/bin/tmux")
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout="keep-1\t/work\nodd.name\t/x\n" + "n" * 40 + "\t/y\n", stderr="")
    monkeypatch.setattr(native_app.subprocess, "run", fake_run)
    monkeypatch.delenv("TETHER_TMUX_SOCKET", raising=False)
    assert native_app.list_sessions()["sessions"] == [{"name": "keep-1", "cwd": "/work"}]
    assert calls[-1][:3] == ["/usr/bin/tmux", "-L", "tether"]
    assert native_app.end_session("keep-1") == {"protocol": 1, "ended": True}
    assert calls[-1][3:] == ["kill-session", "-t", "=keep-1"]


@pytest.mark.skipif(not native_app.tmux_executable(), reason="tmux is not installed")
def test_native_saved_session_survives_hangup_and_reattaches(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / ".zshrc").write_text("export NATIVE_STARTUP_CHECK=preserved\n")
    # Unix socket paths are limited to about 100 bytes; pytest's tmp_path is longer.
    socket_dir = Path(tempfile.mkdtemp(prefix="tt-", dir="/tmp"))
    socket = socket_dir / "s"
    env = dict(os.environ, HOME=str(home), ZDOTDIR=str(home), TMPDIR=str(tmp_path),
               TETHER_CONFIG_DIR=str(tmp_path / "config"), TETHER_TMUX_SOCKET=str(socket),
               TERM="xterm-256color", PYTHONPATH=str(Path(__file__).resolve().parents[2]))
    env.pop("TMUX", None)
    command = [sys.executable, "-m", "tether", "native-app", "shell", "--theme", "minimal",
               "--cwd", str(home), "--tmux", "keep-1"]

    def helper(*actions):
        result = subprocess.run([sys.executable, "-m", "tether", "native-app", *actions],
                                env=env, capture_output=True, text=True, timeout=10, check=True)
        return json.loads(result.stdout)

    def attach():
        master, slave = os.openpty()
        child = subprocess.Popen(command, stdin=slave, stdout=slave, stderr=slave, env=env, start_new_session=True)
        os.close(slave)
        return child, master

    def hangup(child, master):
        if child.poll() is None:
            os.kill(child.pid, signal.SIGHUP)
        deadline = time.monotonic() + 5
        while child.poll() is None and time.monotonic() < deadline:
            if select.select([master], [], [], 0.05)[0]:
                try:
                    os.read(master, 65536)
                except OSError:
                    break
        os.close(master)
        return child.wait(timeout=2)

    child, master = attach()
    try:
        os.write(master, b"printf '\\nFIRST:%s:%s\\n' \"$NATIVE_STARTUP_CHECK\" \"${TMUX:+tmux}\"; export NATIVE_MARK=kept\n")
        _read_until(master, b"FIRST:preserved:tmux")
        assert hangup(child, master) >= 0
        listing = helper("sessions")
        assert listing["tmux"] is True
        assert listing["sessions"] == [{"name": "keep-1", "cwd": str(home)}]
        child, master = attach()
        os.write(master, b"printf '\\nAGAIN:%s\\n' \"$NATIVE_MARK\"\n")
        _read_until(master, b"AGAIN:kept")
        assert hangup(child, master) >= 0
        assert helper("end-session", "--tmux", "keep-1") == {"protocol": 1, "ended": True}
        assert helper("sessions")["sessions"] == []
        assert not list(tmp_path.glob("tether-tmux-*")), "private tmux configuration was not cleaned up"
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)
        subprocess.run([native_app.tmux_executable(), "-S", str(socket), "kill-server"],
                       capture_output=True, timeout=5)
        shutil.rmtree(socket_dir, ignore_errors=True)
