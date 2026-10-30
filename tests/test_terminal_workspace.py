"""Exercise real terminal event handling without contacting a model provider."""
import asyncio
import threading
import time
from types import SimpleNamespace

import pytest
from prompt_toolkit.data_structures import Size
from prompt_toolkit.application.current import set_app
from prompt_toolkit.formatted_text import to_plain_text
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.utils import get_cwidth

from tether.cli import build_parser
from tether.core.config import TetherConfig
from tether.session.input_broker import InputBroker
from tether.ui.workspace import TerminalWorkspace, Transcript, provider_identity
from tether.ui.markdown import term_width


class WideOutput(DummyOutput):
    def get_size(self):
        return Size(rows=36, columns=120)


class PaintedOutput(WideOutput):
    """Record cells actually painted, not just the layout's intended styles."""

    def __init__(self):
        self.size = super().get_size()
        self.x = self.y = 0
        self.attrs = None
        self.cells = {}

    def get_size(self):
        return self.size

    def set_attributes(self, attrs, color_depth):
        self.attrs = attrs

    def reset_attributes(self):
        self.attrs = None

    def erase_down(self):
        self.cells = {position: value for position, value in self.cells.items()
                      if (position[1], position[0]) < (self.y, self.x)}

    def write(self, data):
        for char in data:
            if char == "\r":
                self.x = 0
            elif char == "\n":
                self.y += 1
            else:
                self.cells[self.x, self.y] = (char, self.attrs)
                self.x += get_cwidth(char)

    def cursor_forward(self, amount):
        self.x += amount

    def cursor_backward(self, amount):
        self.x -= amount

    def cursor_up(self, amount):
        self.y -= amount


def render_frame(ui):
    async def render():
        with set_app(ui.app):
            ui.app.render_counter += 1
            ui.app.renderer.render(ui.app, ui.app.layout)
            await ui.app.cancel_and_wait_for_background_tasks()

    asyncio.run(render())


def test_conversation_background_is_painted_on_start_resize_and_sidebar_toggle():
    with create_pipe_input() as pipe:
        output = PaintedOutput()
        ui = TerminalWorkspace(make_repl(), input=pipe, output=output)
        ui.transcript.write("\x1b[96mTETHER\x1b[0m\nReady when you are.\n")
        for columns, rows, sidebar in [(120, 36, True), (60, 20, True), (120, 36, False)]:
            output.size = Size(rows=rows, columns=columns)
            ui.sidebar_visible = sidebar
            render_frame(ui)
            # Untouched cells otherwise retain the user's terminal background.
            for x, y in [(40, 2), (50, rows // 2), (columns - 1, rows - 7)]:
                char, attrs = output.cells[x, y]
                assert attrs.bgcolor == "101116"
                assert not attrs.reverse


def test_agent_selection_uses_one_background_without_inverting_provider_colors():
    with create_pipe_input() as pipe:
        output = PaintedOutput()
        ui = TerminalWorkspace(make_repl(), input=pipe, output=output)
        render_frame(ui)
        _, title = output.cells[10, 8]
        _, subtitle = output.cells[10, 9]
        assert title.bgcolor == subtitle.bgcolor == "2c293d"
        assert not title.reverse and not subtitle.reverse
        assert title.color == "e3a17e"
        assert subtitle.color == "9193a5"


def test_settings_dialog_keeps_buttons_visible_in_a_small_terminal():
    with create_pipe_input() as pipe:
        output = PaintedOutput()
        output.size = Size(rows=20, columns=60)
        ui = TerminalWorkspace(make_repl(), input=pipe, output=output)
        ui.open_settings()
        render_frame(ui)
        screen = ui.app.renderer.last_rendered_screen
        text = "\n".join("".join(screen.data_buffer[y][x].char for x in range(60)) for y in range(20))
        assert "Shell & appearance" in text and "Project folder" in text and "Close" in text
        assert "Window too small" not in text
        top = next(y for y in range(20) if "┐" in text.splitlines()[y])
        bottom = next(y for y in range(top + 1, 20) if "┘" in text.splitlines()[y])
        right = next(x for x in range(60) if screen.data_buffer[top][x].char == "┐")
        assert all(screen.data_buffer[y][right].char == "│" for y in range(top + 1, bottom))


def test_settings_does_not_change_project_during_agent_work(tmp_path):
    with create_pipe_input() as pipe:
        ui = TerminalWorkspace(make_repl(), input=pipe, output=WideOutput())
        original = ui.file_tree.root
        ui.update_agent(dict(agent_id="background", status="running", revision=1))
        ui.open_settings()
        ui.open_directory_settings()
        ui._request_directory(str(tmp_path), False)
        assert ui.dialog is not None and "running agents" in ui.dialog.message
        assert ui.inbox.empty() and ui.file_tree.root == original


@pytest.mark.parametrize("dependency", ["ptterm", "pyte"])
def test_missing_shell_dependency_preserves_layout_and_shows_recovery(monkeypatch, tmp_path, dependency):
    import builtins

    original_import = builtins.__import__

    def missing_import(name, *args, **kwargs):
        if name == "ptterm":
            raise ModuleNotFoundError(f"No module named '{dependency}'", name=dependency)
        return original_import(name, *args, **kwargs)

    with create_pipe_input() as pipe:
        ui = TerminalWorkspace(make_repl(), input=pipe, output=PaintedOutput())
        file = tmp_path / "example.txt"
        file.write_text("Still readable")
        preview = ui.split_pane("right", file=file)
        before = ui.panes.root
        focus = ui.app.layout.current_control
        monkeypatch.setattr(builtins, "__import__", missing_import)
        for direction in ("left", "right", "up", "down"):
            assert ui.split_pane(direction) is None
            assert ui.panes.root is before and ui.panes.active is preview
            assert ui.app.layout.current_control is focus
            assert len(ui.panes.leaves()) == len(ui._pane_widgets) == 2
        assert "Reinstall Tether" in to_plain_text(ui._pane_toolbar())
        render_frame(ui)
        ui.close_pane()
        assert ui.panes.active is ui.host_pane


def test_failed_terminal_setup_closes_allocated_pty(monkeypatch):
    import ptterm
    from tether.ui.terminal_backend import ShellBackend

    created = []

    def fail_terminal(*, backend, **kwargs):
        assert isinstance(backend, ShellBackend)
        created.append(backend)
        raise RuntimeError("terminal setup failed")

    monkeypatch.setattr(ptterm, "Terminal", fail_terminal)

    async def check():
        with create_pipe_input() as pipe:
            ui = TerminalWorkspace(make_repl(), input=pipe, output=WideOutput())
            assert ui.split_pane("down") is None
            assert ui.panes.root is ui.host_pane
            assert ui.app.layout.has_focus(ui.buffer)
            assert created[0].closed and created[0].master is None and created[0].slave is None
            assert "terminal setup failed" in ui.notice

    asyncio.run(check())


def make_repl():
    return SimpleNamespace(
        config=TetherConfig(provider="anthropic", api_model="claude-test"),
        _broker=InputBroker(), _controller=None, _engine=None,
        _history=None, skill_store=None,
    )


def test_transcript_keeps_colors_and_bounds_progress_and_history():
    transcript = Transcript(max_lines=2)
    transcript.write("old\nkeep\n\x1b[31mred\x1b[0m\nprogress\rreplaced\x1b[K")
    assert transcript.text() == "keep\n\x1b[31mred\x1b[0m\nreplaced"
    transcript.write("x" * 25000)
    assert len(transcript.text().splitlines()[-1]) == 24000


def test_provider_marks_are_distinct_and_always_have_a_name():
    assert provider_identity("anthropic")[1] == "Claude"
    assert provider_identity("openai")[1] == "GPT"
    assert provider_identity("codex")[1] == "Codex"
    assert provider_identity("custom")[1] == "Custom"
    assert provider_identity("anthropic")[0] != provider_identity("openai")[0]


def test_sidebar_preserves_agent_provider_and_ignores_stale_updates():
    with create_pipe_input() as pipe:
        ui = TerminalWorkspace(make_repl(), input=pipe, output=WideOutput())
        ui.update_agent(dict(agent_id="a", label="Agent 1", provider="openai", model="gpt-test",
                             agent_number=1, revision=2, status="completed", output="Done"))
        ui.update_agent(dict(agent_id="a", revision=1, status="running"))
        sidebar = to_plain_text(ui._sidebar())
        assert "Claude" in sidebar and "GPT" in sidebar
        assert "completed" in sidebar and "gpt-test" in sidebar
        ui._select_relative(1)
        assert ui.selected == "a" and "Done" in ui._view_text()
        ui._select_relative(1)
        assert ui.selected == "host"


def test_real_application_streams_while_accepting_followup_and_approval():
    repl = make_repl()
    seen = []
    with create_pipe_input() as pipe:
        ui = TerminalWorkspace(repl, input=pipe, output=WideOutput())

        def session():
            assert term_width() == 90
            pipe.send_text("hello")
            # Enter separately so the REPL's non-bracketed-paste guard applies.
            time.sleep(0.1)
            pipe.send_text("\r")
            seen.append(ui.read_line(timeout=3))
            print("streamed output")
            pipe.send_text("followup")
            time.sleep(0.1)
            pipe.send_text("\r")
            seen.append(ui.read_line(timeout=3))

            def answer():
                deadline = time.monotonic() + 3
                while not repl._broker.has_pending() and time.monotonic() < deadline:
                    time.sleep(0.01)
                pipe.send_text("y")
                time.sleep(0.1)
                pipe.send_text("\r")

            responder = threading.Thread(target=answer)
            responder.start()
            seen.append(repl._broker.request_line("approve bash?"))
            responder.join(3)
            return 0

        assert ui.run(session) == 0
        assert seen == ["hello", "followup", "y"]
        assert "streamed output" in ui.transcript.text()
        assert "hello" in ui.transcript.text()
        assert ui.inbox.get_nowait().__class__ is EOFError  # shutdown unblocks readers


def test_ctrl_d_cancels_a_pending_approval_and_restores_terminal():
    repl = make_repl()
    seen = []
    with create_pipe_input() as pipe:
        ui = TerminalWorkspace(repl, input=pipe, output=WideOutput())

        def session():
            def quit_when_pending():
                deadline = time.monotonic() + 3
                while not repl._broker.has_pending() and time.monotonic() < deadline:
                    time.sleep(0.01)
                pipe.send_text("\x04")

            sender = threading.Thread(target=quit_when_pending)
            sender.start()
            seen.append(repl._broker.request_line("approval"))
            sender.join(3)
            return 0

        assert ui.run(session) == 0
        assert seen == [None]
        assert ui.closed.is_set()


def test_plain_flag_works_with_default_start_and_run():
    for argv in (["--plain"], ["start", "--plain"], ["run", "--plain"], ["--plain", "start"]):
        assert build_parser().parse_args(argv).plain is True
    assert build_parser().parse_args([]).plain is False


def wait_for(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert predicate(), "Timed out waiting for terminal state"


def terminal_text(pane):
    screen = pane.terminal.process.screen
    return "\n".join("".join(cell.char for _, cell in sorted(row.items()))
                     for _, row in sorted(screen.data_buffer.items()))


def test_real_shell_input_interrupt_resize_and_shutdown(tmp_path, monkeypatch):
    import shlex
    import sys

    monkeypatch.setenv("SHELL", "/bin/sh")
    monkeypatch.setenv("TETHER_CONFIG_DIR", str(tmp_path / "config"))
    fixture = tmp_path / "terminal_app.py"
    fixture.write_text("""import os, sys, termios, tty
saved = termios.tcgetattr(0)
try:
    tty.setraw(0)
    sys.stdout.write('\\x1b[?1049h\\x1b[2J\\x1b[HAgent UI fixture')
    sys.stdout.flush()
    char = os.read(0, 1)
    sys.stdout.write('\\r\\nkey:' + char.decode())
    sys.stdout.flush()
    os.read(0, 1)
finally:
    sys.stdout.write('\\x1b[?1049l')
    sys.stdout.flush()
    termios.tcsetattr(0, termios.TCSANOW, saved)
""")
    with create_pipe_input() as pipe:
        output = PaintedOutput()
        ui = TerminalWorkspace(make_repl(), input=pipe, output=output)
        ui.file_tree.set_root(tmp_path)
        shells = []

        def session():
            pipe.send_text("\x17\x1b[C")  # Ctrl+W, Right
            wait_for(lambda: len(ui.panes.leaves()) == 2 and ui.panes.active.terminal is not None)
            shell = ui.panes.active
            shells.append(shell)
            wait_for(lambda: shell.terminal.process.backend.process is not None)
            pipe.send_text("printf 'split-%s\\n' ready; pwd\r")
            wait_for(lambda: "split-ready" in terminal_text(shell))
            assert str(tmp_path) in terminal_text(shell).replace("\n", "")
            assert ui.inbox.empty() and not ui.buffer.text
            pipe.send_text("sleep 30\r")
            time.sleep(0.15)
            pipe.send_text("\x03")
            pipe.send_text("printf 'interrupt-%s\\n' ok\r")
            wait_for(lambda: "interrupt-ok" in terminal_text(shell))
            pipe.send_text(f"{shlex.quote(sys.executable)} {shlex.quote(str(fixture))}\r")
            wait_for(lambda: "Agent UI fixture" in terminal_text(shell))
            pipe.send_text("h")
            wait_for(lambda: "key:h" in terminal_text(shell))
            pipe.send_text("q")
            wait_for(lambda: "Agent UI fixture" not in terminal_text(shell))
            old_width = shell.terminal.process.sx
            output.size = Size(rows=44, columns=150)
            ui.invalidate()
            wait_for(lambda: shell.terminal.process.sx != old_width)
            pipe.send_text("stty size\r")
            expected = f"{shell.terminal.process.sy} {shell.terminal.process.sx}"
            wait_for(lambda: expected in terminal_text(shell))
            # A second directional split does not steal Ctrl+C from a shell.
            pipe.send_text("\x17\x1b[B")
            wait_for(lambda: len(ui.panes.leaves()) == 3 and ui.panes.active.terminal is not None)
            shells.append(ui.panes.active)
            pipe.send_text("printf 'second-%s\\n' shell\r")
            wait_for(lambda: "second-shell" in terminal_text(shells[-1]))
            pipe.send_text("trap '' HUP; sleep 30\r")
            time.sleep(0.1)
            pipe.send_text("\x17x")
            wait_for(lambda: len(ui.panes.leaves()) == 2)
            return 0

        assert ui.run(session) == 0
        for shell in shells:
            backend = shell.terminal.process.backend
            wait_for(lambda: backend.process.poll() is not None)
            assert backend.closed


def test_settings_keyboard_navigation_and_file_preview(tmp_path):
    from pathlib import Path

    (tmp_path / "hello.txt").write_text("File tree preview\n")
    with create_pipe_input() as pipe:
        ui = TerminalWorkspace(make_repl(), input=pipe, output=PaintedOutput())

        def session():
            pipe.send_text("\x1bOQ")  # F2
            wait_for(lambda: ui.dialog is not None)
            wait_for(lambda: ui.dialog.buttons[0].window in
                     ui.app.renderer.last_rendered_screen.visible_windows_to_write_positions)
            pipe.send_text("\t\r")  # Project folder in the settings menu
            wait_for(lambda: hasattr(ui.dialog, "path"))
            ui.app.loop.call_soon_threadsafe(setattr, ui.dialog.path, "text", str(tmp_path))
            time.sleep(0.1)
            ui.app.loop.call_soon_threadsafe(ui.dialog.open)
            requested = ui.read_line(timeout=3)
            assert requested == Path(tmp_path)
            ui.directory_changed(requested)
            wait_for(lambda: ui.files_visible and ui.file_tree.root == tmp_path)
            ui.app.loop.call_soon_threadsafe(ui.app.layout.focus, ui.tree_control)
            time.sleep(0.1)
            pipe.send_text("\r")
            wait_for(lambda: ui.panes.active.file is not None)
            assert "File tree preview" in ui.panes.active.preview
            pipe.send_text("\x1bOQ")
            wait_for(lambda: ui.dialog is not None)
            pipe.send_text("\x1b")
            wait_for(lambda: ui.dialog is None)
            assert ui.panes.active.file.name == "hello.txt"
            return 0

        assert ui.run(session) == 0


def test_mouse_toggle_hands_selection_back_to_the_terminal():
    with create_pipe_input() as pipe:
        ui = TerminalWorkspace(make_repl(), input=pipe, output=PaintedOutput())
        assert not ui.app.mouse_support()
        assert "Mouse" in to_plain_text(ui._pane_toolbar())
        ui.toggle_mouse()
        assert ui.app.mouse_support() and "F10" in ui.notice
        assert "Mouse on" in to_plain_text(ui._pane_toolbar())
        render_frame(ui)
        ui.toggle_mouse()
        assert not ui.app.mouse_support() and "select" in ui.notice
