"""Internal native-terminal adapters; agent and theme behavior stays in Python."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import termios
import time

from .core.config import TetherConfig
from .ui.shell_theme import ShellTheme, save_settings
from .ui.theme_catalog import theme_catalog

_MAX_INPUT = 16 * 1024
_MAX_OUTPUT = 1024 * 1024
_FIELDS = {"shell", "theme", "font", "theme_file"}
# tmux session names are host-generated tokens; '.', ':' and spaces would be
# interpreted as window or pane targets, so they are never accepted.
_SESSION_NAME = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
_TMUX_SOCKET_NAME = "tether"
# Theme variables that must reach the shell inside a saved tmux session.
_THEME_KEYS = ("ZDOTDIR", "TETHER_ZSH_BOOTSTRAP", "TETHER_ZSH_USER_DIR", "TETHER_ZSH_USER_DIR_SET",
               "TETHER_ZSH_THEME_FILE", "TETHER_OMZ_ROOT", "TETHER_OMZ_BUNDLED")
# Private server configuration: Tether draws the panes and tabs, so tmux's own
# status line stays off. Mouse mode scrolls history and selects text; copies
# reach the macOS pasteboard through OSC 52, which the display handles.
_TMUX_CONF = """\
set -g default-terminal "xterm-256color"
set -as terminal-overrides ",xterm-256color:RGB"
set -as terminal-overrides ',xterm-256color:Ms=\\E]52;%p1%s;%p2%s\\007'
set -s set-clipboard on
set -g mouse on
set -g status off
set -g history-limit 20000
set -s escape-time 10
set -g focus-events on
set -g exit-empty on
"""


def tmux_executable():
    return shutil.which("tmux")


def validate_session(name):
    if not isinstance(name, str) or not _SESSION_NAME.match(name):
        raise ValueError("Session names use 1-32 letters, digits, '-' or '_'.")
    return name


def tmux_command(tmux, *arguments):
    socket = os.environ.get("TETHER_TMUX_SOCKET", "")
    selector = ["-S", socket] if socket else ["-L", _TMUX_SOCKET_NAME]
    return [tmux, *selector, *arguments]


def _tmux(tmux, *arguments):
    return subprocess.run(tmux_command(tmux, *arguments), capture_output=True, text=True, timeout=5)


def list_sessions():
    """Saved shells on Tether's private tmux server; never the user's own tmux."""
    tmux = tmux_executable()
    if not tmux:
        return {"protocol": 1, "tmux": False, "sessions": []}
    result = _tmux(tmux, "list-sessions", "-F", "#{session_name}\t#{session_path}")
    sessions = []
    if result.returncode == 0:
        for line in result.stdout.splitlines()[:64]:
            name, _, path = line.partition("\t")
            if _SESSION_NAME.match(name):
                sessions.append({"name": name, "cwd": path})
    return {"protocol": 1, "tmux": True, "sessions": sessions}


def end_session(name):
    validate_session(name)
    tmux = tmux_executable()
    if not tmux:
        raise ValueError("tmux is not installed.")
    result = _tmux(tmux, "kill-session", "-t", "=" + name)
    return {"protocol": 1, "ended": result.returncode == 0}


def tmux_attach_command(session, shell, env, cwd, conf):
    """Attach to a saved session, or start it with the themed login shell.

    A reattached session receives the fresh theme directory so windows the user
    opens later with the tmux prefix still find their startup files."""
    validate_session(session)
    tmux = tmux_executable()
    if not tmux:
        raise ValueError("Saved sessions need tmux. Install it with: brew install tmux")
    keys = [key for key in _THEME_KEYS if key in env]
    if _tmux(tmux, "has-session", "-t", "=" + session).returncode == 0:
        for key in keys:
            _tmux(tmux, "set-environment", "-t", "=" + session, key, env[key])
        return tmux_command(tmux, "attach-session", "-t", "=" + session)
    command = tmux_command(tmux, "-f", conf, "new-session", "-s", session, "-c", cwd)
    for key in keys:
        command += ["-e", f"{key}={env[key]}"]
    # New windows inside the session keep using the selected shell.
    return command + [shell, "-l", ";", "set-option", "default-shell", shell]


def settings_payload(config):
    return {
        "protocol": 1,
        "shell": config.terminal_shell,
        "theme": config.terminal_theme,
        "font": config.terminal_font_mode,
        "theme_file": config.terminal_theme_file,
        "home": str(Path.home()),
        "themes": [{"id": item.id, "name": item.name, "source": item.source}
                   for item in theme_catalog()],
    }


def write_json(payload):
    encoded = json.dumps(payload, ensure_ascii=True)
    if len(encoded) > _MAX_OUTPUT:
        raise ValueError("Settings catalog exceeds the native protocol limit.")
    sys.stdout.write(encoded + "\n")
    sys.stdout.flush()


def read_settings(stream):
    data = stream.readline(_MAX_INPUT + 1)
    if len(data.encode("utf-8") if isinstance(data, str) else data) > _MAX_INPUT:
        raise ValueError("Settings input exceeds 16 KiB.")
    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate settings key.")
            result[key] = value
        return result
    value = json.loads(data, object_pairs_hook=unique_pairs)
    if not isinstance(value, dict) or set(value) != _FIELDS:
        raise ValueError("Expected exactly shell, theme, font, and theme_file.")
    if any(not isinstance(item, str) or "\0" in item or len(item) > 4096 for item in value.values()):
        raise ValueError("Settings must be strings of at most 4096 characters without NUL bytes.")
    return value


def claim_terminal():
    """Account for PyInstaller's extra child before acquiring a controlling PTY."""
    if not os.isatty(0):
        raise ValueError("This action requires a terminal.")
    if os.getsid(0) != os.getpid():
        os.setsid()
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)


def _foreground(group):
    previous = signal.signal(signal.SIGTTOU, signal.SIG_IGN)
    try:
        os.tcsetpgrp(0, group)
    finally:
        signal.signal(signal.SIGTTOU, previous)


def run_shell(config, override=None, session=None):
    claim_terminal()
    theme_name = override if override is not None else config.terminal_theme
    shell = "zsh" if override is not None and override != "inherit" else config.terminal_shell
    appearance = ShellTheme(shell, theme_name, config.terminal_font_mode, config.terminal_theme_file)
    child = None
    previous_handlers = {}
    stopping_at = None
    private = None

    def forward(signum, _frame=None):
        nonlocal stopping_at
        if signum in {signal.SIGHUP, signal.SIGTERM} and stopping_at is None:
            stopping_at = time.monotonic()
        if child is None:
            return
        groups = {child.pid}
        try:
            groups.add(os.tcgetpgrp(0))
        except OSError:
            pass
        for group in groups:
            if group > 0 and group != os.getpgrp():
                try:
                    # Interactive shells commonly ignore TERM. HUP asks the
                    # shell to hang up its background jobs before it exits.
                    delivered = signal.SIGHUP if group == child.pid and signum == signal.SIGTERM else signum
                    os.killpg(group, delivered)
                except ProcessLookupError:
                    pass

    try:
        env = appearance.environment(dict(os.environ))
        env.pop("TETHER_NATIVE_ACTIVITY", None)
        if getattr(sys, "frozen", False):
            for name in ("LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH"):
                original = env.pop(name + "_ORIG", None)
                env.pop(name, None)
                if original:
                    env[name] = original
        # The native host provides the bundled command directory in PATH.
        # Startup files stay under the user's control and are never rewritten.
        for sig in (signal.SIGHUP, signal.SIGTERM, signal.SIGINT):
            previous_handlers[sig] = signal.signal(sig, forward)
        # The shell owns the foreground group; wrapper stays alive only to own
        # theme resources. A fresh Python helper has no worker threads here.
        group_options = {"process_group": 0} if sys.version_info >= (3, 11) else {"preexec_fn": os.setpgrp}
        command = [appearance.shell, "-l"]
        if session:
            private = tempfile.TemporaryDirectory(prefix="tether-tmux-")
            conf = Path(private.name) / "tmux.conf"
            conf.write_text(_TMUX_CONF)
            command = tmux_attach_command(session, appearance.shell, env, os.getcwd(), str(conf))
        child = subprocess.Popen(command, env=env, **group_options)
        _foreground(child.pid)
        try:
            os.killpg(child.pid, signal.SIGCONT)
        except ProcessLookupError:
            pass
        while True:
            try:
                code = child.wait(timeout=0.2)
                return code if code >= 0 else 128 - code
            except subprocess.TimeoutExpired:
                if stopping_at is not None and time.monotonic() - stopping_at >= 2:
                    forward(signal.SIGKILL)
    finally:
        if child is not None and child.poll() is None:
            forward(signal.SIGHUP)
            try:
                child.wait(timeout=2)
            except subprocess.TimeoutExpired:
                forward(signal.SIGKILL)
                child.wait(timeout=2)
        try:
            _foreground(os.getpgrp())
        except OSError:
            pass
        for sig, previous in previous_handlers.items():
            signal.signal(sig, previous)
        appearance.close()
        if private is not None:
            private.cleanup()


def run_native_app(args):
    try:
        if args.cwd:
            target = Path(args.cwd).expanduser().resolve(strict=True)
            if not target.is_dir():
                raise ValueError("The working directory must be a folder.")
            os.chdir(target)
        if args.action == "agent":
            from .cli import cmd_start
            claim_terminal()
            os.environ["TETHER_NATIVE_ACTIVITY"] = "1"
            return cmd_start(argparse.Namespace(compact=True, plain=False, port=None,
                            model=None, gguf_dirs=[], resume="", resume_last=False))
        if args.action == "sessions":
            write_json(list_sessions())
            return 0
        if args.action == "end-session":
            write_json(end_session(getattr(args, "session", None)))
            return 0
        config = TetherConfig.load()
        if args.action == "shell":
            return run_shell(config, args.theme, session=getattr(args, "session", None) or None)
        if args.action == "save-settings":
            stream = getattr(sys.stdin, "buffer", sys.stdin)
            values = read_settings(stream)
            save_settings(config, values["shell"], values["theme"], values["font"], values["theme_file"])
        write_json(settings_payload(config))
        return 0
    except (OSError, ValueError, TypeError, subprocess.SubprocessError) as exc:
        if args.action in {"settings", "save-settings", "sessions", "end-session"}:
            # Never echo malformed input or config contents to the host.
            message = "Invalid settings JSON." if isinstance(exc, (json.JSONDecodeError, UnicodeDecodeError)) else str(exc)
            write_json({"protocol": 1, "error": message[:512]})
        else:
            print(f"Tether: {exc}", file=sys.stderr)
        return 2
