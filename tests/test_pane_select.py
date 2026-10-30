"""Drag-to-copy and click-to-paste inside terminal panes."""
from collections import defaultdict
from types import SimpleNamespace
from unittest.mock import Mock

from prompt_toolkit.data_structures import Point
from prompt_toolkit.layout.controls import UIContent
from prompt_toolkit.layout.screen import Char
from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType

from tether.ui.pane_select import PaneSelection, export_clipboard


def fake_terminal(lines):
    buffer = defaultdict(dict)
    for y, line in enumerate(lines):
        for x, char in enumerate(line):
            buffer[y][x] = Char(char, "fg:#ffffff")
    pt_screen = SimpleNamespace(data_buffer=buffer)

    def get_line(number):
        row = buffer.get(number)
        return [(row[x].style, row[x].char) for x in range(max(row) + 1)] if row else [("", " ")]

    control = SimpleNamespace(create_content=lambda width, height: UIContent(get_line=get_line, line_count=len(lines),
                                                                             cursor_position=Point(0, 0)))
    return SimpleNamespace(terminal_control=control, process=SimpleNamespace(
        screen=SimpleNamespace(pt_screen=pt_screen), write_input=Mock()))


def event(kind, x, y, button=MouseButton.LEFT):
    return MouseEvent(Point(x, y), kind, button, frozenset())


def test_drag_highlights_then_copies_the_selected_cells():
    terminal = fake_terminal(["abcdefg", "hijklmn   ", "opq"])
    copied, pasted = Mock(), Mock()
    selection = PaneSelection(terminal, copied, pasted)
    assert selection.handle(event(MouseEventType.MOUSE_DOWN, 2, 0))
    assert selection.handle(event(MouseEventType.MOUSE_MOVE, 3, 1))
    content = terminal.terminal_control.create_content(80, 24)
    first, second, third = (content.get_line(n) for n in range(3))
    assert "pane-selection" not in first[1][0] and all("pane-selection" in f[0] for f in first[2:])
    assert all("pane-selection" in f[0] for f in second[:4]) and "pane-selection" not in second[4][0]
    assert not any("pane-selection" in f[0] for f in third)
    assert content.line_count == 3 and content.cursor_position == Point(0, 0)
    copied.assert_not_called()
    assert selection.handle(event(MouseEventType.MOUSE_UP, 3, 1))
    copied.assert_called_once_with("cdefg\nhijk")
    assert selection.bounds is None
    assert terminal.terminal_control.create_content(80, 24).get_line(0) == [("fg:#ffffff", c) for c in "abcdefg"]


def test_backwards_drag_trailing_blanks_and_plain_clicks():
    terminal = fake_terminal(["abcdefg", "hijklmn   ", "opq"])
    copied = Mock()
    selection = PaneSelection(terminal, copied, Mock())
    selection.handle(event(MouseEventType.MOUSE_DOWN, 1, 2))
    selection.handle(event(MouseEventType.MOUSE_MOVE, 5, 1))
    selection.handle(event(MouseEventType.MOUSE_UP, 5, 1))
    copied.assert_called_once_with("mn\nop")
    copied.reset_mock()
    selection.handle(event(MouseEventType.MOUSE_DOWN, 4, 0))
    selection.handle(event(MouseEventType.MOUSE_UP, 4, 0))
    copied.assert_not_called()
    assert not selection.handle(event(MouseEventType.MOUSE_MOVE, 6, 0, MouseButton.NONE))
    assert not selection.handle(event(MouseEventType.SCROLL_DOWN, 0, 0, MouseButton.NONE))


def test_right_and_middle_click_paste_without_starting_a_selection():
    terminal = fake_terminal(["abc"])
    copied, pasted = Mock(), Mock()
    selection = PaneSelection(terminal, copied, pasted)
    assert selection.handle(event(MouseEventType.MOUSE_DOWN, 0, 0, MouseButton.RIGHT))
    assert selection.handle(event(MouseEventType.MOUSE_UP, 0, 0, MouseButton.RIGHT))
    assert selection.handle(event(MouseEventType.MOUSE_UP, 1, 0, MouseButton.MIDDLE))
    assert pasted.call_args_list == [((terminal,),), ((terminal,),)]
    copied.assert_not_called()
    assert selection.bounds is None


def test_export_sends_osc52_and_runs_a_clipboard_tool(monkeypatch):
    output = Mock()
    ran = []

    class Thread:
        def __init__(self, target, **kwargs):
            self.target = target

        def start(self):
            self.target()

    monkeypatch.setattr("tether.ui.pane_select.threading.Thread", Thread)
    monkeypatch.setattr("tether.ui.pane_select.subprocess.run", lambda *args, **kwargs: ran.append((args, kwargs)))
    export_clipboard("hello", output, tool=["copy-tool"])
    output.write_raw.assert_called_once_with("\x1b]52;c;aGVsbG8=\x07")
    assert ran[0][0] == (["copy-tool"],) and ran[0][1]["input"] == b"hello"
    output.reset_mock()
    export_clipboard("x" * 200_000, output, tool=[])
    output.write_raw.assert_not_called()
