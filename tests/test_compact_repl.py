"""The scrolling REPL's compact session look (the macOS app's Agent pane)."""
import argparse
import shutil
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tether import cli
from tether.core.config import TetherConfig
from tether.ui import repl as repl_module
from tether.ui.repl import TetherREPL


def compact_repl(config, compact=True):
    repl = TetherREPL.__new__(TetherREPL)
    repl.config = config
    repl._workspace = None
    repl._compact = compact
    config.save = Mock()
    return repl


def engine(tokens=0, plan=False):
    return SimpleNamespace(_estimate_message_tokens=lambda: tokens, plan_mode=plan, turn_count=0)


@pytest.mark.parametrize("flags, expected", [
    ([], {"plain": False}),
    (["--plain"], {"plain": True, "compact": True}),
    (["--compact"], {"plain": True, "compact": True}),
    (["--plain", "--classic"], {"plain": True}),
    (["--compact", "--classic"], {"plain": True}),
])
def test_scrolling_repl_is_compact_unless_classic(flags, expected):
    args = cli.build_parser().parse_args(["start", *flags])
    assert cli._repl_options(args) == expected
    args = cli.build_parser().parse_args(["run", *flags] if "--compact" not in flags else ["start", *flags])
    assert cli._repl_options(args) == expected


def test_top_level_flags_reach_the_default_command():
    args = cli.build_parser().parse_args(["--plain", "--classic"])
    assert cli._repl_options(args) == {"plain": True}
    native = argparse.Namespace(compact=True, plain=False)  # native-app agent launch
    assert cli._repl_options(native) == {"plain": True, "compact": True}


def test_compact_welcome_is_a_small_session_heading(capsys):
    repl = compact_repl(TetherConfig(provider="codex", api_model="gpt-6-astra", context_size=1_000_000))
    repl._print_welcome("Good morning")
    out = capsys.readouterr().out
    assert "◇" in out and "tether" in out and "/ Codex" in out
    assert "What would you like to work on?" in out
    assert "/help commands  ·  /plan plan first  ·  /resume continue" in out
    assert "Good morning" not in out and "1,000,000 context" not in out


def test_classic_welcome_is_unchanged(capsys):
    repl = compact_repl(TetherConfig(provider="codex", api_model="gpt-6-astra", context_size=1_000_000),
                        compact=False)
    repl._print_welcome("Good morning")
    out = capsys.readouterr().out
    assert "Good morning." in out and "1,000,000 context" in out
    assert "What would you like to work on?" not in out


def test_compact_status_shows_model_effort_context_and_hint(monkeypatch):
    monkeypatch.setattr(repl_module, "reasoning_setting", lambda config: ("", ["low", "high"], "low"))
    monkeypatch.setattr(shutil, "get_terminal_size", lambda: SimpleNamespace(columns=120, lines=40))
    repl = compact_repl(TetherConfig(provider="codex", api_model="gpt-6-astra", context_size=1000))
    [(style, label)] = repl._bottom_toolbar(engine(tokens=250))
    assert style == "class:bottom-toolbar"
    assert label == "  gpt-6-astra · low · 25% context    Enter send · Alt+Enter newline"
    # Narrow terminals drop the key hint, never the status.
    monkeypatch.setattr(shutil, "get_terminal_size", lambda: SimpleNamespace(columns=40, lines=40))
    [(_, label)] = repl._bottom_toolbar(engine(tokens=250, plan=True))
    assert label == "  PLAN · gpt-6-astra · low · 25% context"


def test_compact_status_omits_effort_without_choices(monkeypatch):
    monkeypatch.setattr(repl_module, "reasoning_setting", lambda config: ("", [], "default"))
    repl = compact_repl(TetherConfig(provider="codex", api_model="gpt-6-astra", context_size=1000))
    [(_, label)] = repl._bottom_toolbar(engine())
    assert label.startswith("  gpt-6-astra · 0% context")


def test_compact_prompt_is_a_chevron():
    repl = compact_repl(TetherConfig(provider="codex", api_model="gpt-6-astra"))
    assert "›" in repl._prompt(engine()) and ">" not in repl._prompt(engine())
    assert ">" in compact_repl(TetherConfig(provider="codex", api_model="gpt-6-astra"), compact=False)._prompt(engine())


@pytest.mark.parametrize("compact", [True, False])
def test_turn_footer_is_a_quiet_line_in_compact_mode(capsys, monkeypatch, compact):
    repl = compact_repl(TetherConfig(provider="codex", api_model="gpt-6-astra"), compact=compact)
    repl.history = Mock()
    repl.codebase_model = None
    repl._last_total = 0
    repl.session = SimpleNamespace(messages=[], cwd="", input_tokens=0, output_tokens=0)
    repl._engine = SimpleNamespace(usage=SimpleNamespace(total=1200, input_tokens=1000, output_tokens=200),
                                   messages=[], _turn_low_confidence=[], _suggest_skill=None, backend=None)
    monkeypatch.setattr(repl_module, "save_session", lambda session: None)
    repl._on_turn_complete(SimpleNamespace(tool_calls_made=[1, 2], stop_reason="completed"), 4.2)
    out = capsys.readouterr().out
    assert "4.2s · 1.2k tok · 2 tools" in out
    assert ("─" in out) is (not compact)
