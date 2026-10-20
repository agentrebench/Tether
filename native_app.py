"""Internal native-terminal adapters; agent and theme behavior stays in Python."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import termios
import time

from .core.config import TetherConfig
from .ui.shell_theme import ShellTheme, save_settings
from .ui.theme_catalog import theme_catalog

_MAX_INPUT = 16 * 1024
_MAX_OUTPUT = 1024 * 1024
_FIELDS = {"shell", "theme", "font", "theme_file"}


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


def run_shell(config, override=None):
    claim_terminal()
    theme_name = override if override is not None else config.terminal_theme
    shell = "zsh" if override is not None and override != "inherit" else config.terminal_shell
    appearance = ShellTheme(shell, theme_name, config.terminal_font_mode, config.terminal_theme_file)
    child = None
    previous_handlers = {}
    stopping_at = None

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
        child = subprocess.Popen([appearance.shell, "-l"], env=env, **group_options)
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
        config = TetherConfig.load()
        if args.action == "shell":
            return run_shell(config, args.theme)
        if args.action == "save-settings":
            stream = getattr(sys.stdin, "buffer", sys.stdin)
            values = read_settings(stream)
            save_settings(config, values["shell"], values["theme"], values["font"], values["theme_file"])
        write_json(settings_payload(config))
        return 0
    except (OSError, ValueError, TypeError) as exc:
        if args.action in {"settings", "save-settings"}:
            # Never echo malformed input or config contents to the host.
            message = "Invalid settings JSON." if isinstance(exc, (json.JSONDecodeError, UnicodeDecodeError)) else str(exc)
            write_json({"protocol": 1, "error": message[:512]})
        else:
            print(f"Tether: {exc}", file=sys.stderr)
        return 2
