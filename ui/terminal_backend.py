"""A PTY backend for ptterm that also works in the standalone executable."""
from __future__ import annotations

import asyncio
import codecs
import errno
import os
from pathlib import Path
import signal
import struct
import subprocess
import sys
import time

from ptterm.backends.base import Backend
from .shell_theme import ShellTheme


def enter_shell(shell: str, command: list[str] | None = None) -> int:
    """Executed in a fresh child, so no Python code runs after a threaded fork."""
    import fcntl
    import termios

    # A one-file bootloader starts a second process before entering Python.
    # That child must become the session leader before claiming the terminal.
    if os.getsid(0) != os.getpid():
        os.setsid()
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)
    os.execv(command[0], command) if command else os.execv(shell, [shell, "-l"])
    return 1


def tether_command(*args):
    prefix = [sys.executable] if getattr(sys, "frozen", False) else [sys.executable, "-m", "tether"]
    return [*prefix, *args]


class ShellBackend(Backend):
    def __init__(self, cwd: Path, shell: str | None = None, *, theme="inherit", font="ascii", theme_file="", command=None):
        self.cwd = Path(cwd)
        self.theme = ShellTheme(shell or "", theme, font, theme_file)
        self.shell = self.theme.shell
        self.command = list(command) if command else None
        self.loop = asyncio.get_running_loop()
        self.ready_f = self.loop.create_future()
        self.master, self.slave = os.openpty()
        os.set_blocking(self.master, False)
        self.process = None
        self.exit_code = None
        self.error = ""
        self._closed = False
        self._kill_requested = False
        self._connected = False
        self._callbacks = []
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._pending = bytearray()
        self._name = Path(self.shell).name
        self._name_checked = 0.0

    @property
    def closed(self):
        return self._closed

    def add_input_ready_callback(self, callback):
        self._callbacks.append(callback)

    def start(self):
        if self.process is not None or self.closed:
            return
        command = tether_command("terminal-shell", "--shell", self.shell)
        if self.command:
            command.extend(["--command", *self.command])
        env = dict(os.environ, TERM="xterm-256color", COLORTERM="truecolor")
        if getattr(sys, "frozen", False):
            env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
            # System executables must not load libraries from the frozen app.
            for name in ("LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH"):
                original = env.pop(name + "_ORIG", None)
                env.pop(name, None)
                if original:
                    env[name] = original
        try:
            env = self.theme.environment(env)
            self.process = subprocess.Popen(
                command,
                cwd=self.cwd, env=env, stdin=self.slave, stdout=self.slave, stderr=self.slave,
                start_new_session=True,
            )
        except OSError as exc:
            self.error = str(exc)
            self.exit_code = 127
            self._dispose()
            self.theme.close()
            self.ready_f.set_result(self.exit_code)
            return
        finally:
            if self.slave is not None:
                os.close(self.slave)
                self.slave = None
        self.loop.create_task(self._wait())

    async def _wait(self):
        self.exit_code = await self.loop.run_in_executor(None, self.process.wait)
        self.theme.close()
        if not self.ready_f.done():
            self.ready_f.set_result(self.exit_code)

    def connect_reader(self):
        if not self.closed and not self._connected:
            self.loop.add_reader(self.master, self._read_ready)
            self._connected = True

    def _read_ready(self):
        for callback in self._callbacks:
            callback()

    def disconnect_reader(self):
        if self._connected:
            self.loop.remove_reader(self.master)
            self._connected = False

    def read_text(self, amount=4096):
        if self.closed:
            return ""
        try:
            data = os.read(self.master, amount)
        except BlockingIOError:
            return ""
        except OSError as exc:
            if exc.errno not in (errno.EIO, errno.EBADF):
                raise
            data = b""
        if not data:
            self._dispose()
            return ""
        return self._decoder.decode(data)

    def write_text(self, text):
        if not self.closed:
            self._pending.extend(text.encode("utf-8"))
            self._write_ready()

    def _write_ready(self):
        while self._pending and not self.closed:
            try:
                count = os.write(self.master, self._pending)
                del self._pending[:count]
            except BlockingIOError:
                self.loop.add_writer(self.master, self._write_ready)
                return
            except OSError:
                self._dispose()
                return
        if self.master is not None:
            self.loop.remove_writer(self.master)

    def set_size(self, width, height):
        import fcntl
        import termios

        if not self.closed:
            fcntl.ioctl(self.master, termios.TIOCSWINSZ,
                        struct.pack("HHHH", max(1, height), max(1, width), 0, 0))

    def _dispose(self):
        if self.closed:
            return
        self.disconnect_reader()
        self.loop.remove_writer(self.master)
        os.close(self.master)
        self.master = None
        if self.slave is not None:
            os.close(self.slave)
            self.slave = None
        self._closed = True

    def kill(self):
        if self._kill_requested:
            return
        self._kill_requested = True
        if self.process is not None and self.process.poll() is None:
            # An interactive job may own a different foreground process group.
            # Leave a frozen executable's bootloader alive to reap its child
            # and remove its temporary extraction directory.
            groups = set() if getattr(sys, "frozen", False) else {self.process.pid}
            if self.master is not None:
                try:
                    foreground = os.tcgetpgrp(self.master)
                    if foreground > 0:
                        groups.add(foreground)
                        session = os.getsid(foreground)
                        if session != os.getsid(0):
                            groups.add(session)
                except OSError:
                    pass
            groups = {group for group in groups if group > 0 and group != os.getpgrp()}

            def signal_groups(sig):
                for group in groups:
                    try:
                        os.killpg(group, sig)
                    except ProcessLookupError:
                        pass

            # Let the interactive shell hang up its own background jobs first.
            signal_groups(signal.SIGHUP)

            def force_close():
                if self.process.poll() is None:
                    signal_groups(signal.SIGKILL)
            self.loop.call_later(0.25, force_close)
        self._dispose()
        if self.process is None and not self.ready_f.done():
            self.ready_f.set_result(None)

    def get_name(self):
        if self.master is not None and time.monotonic() - self._name_checked > 1:
            from ptterm.backends.posix import get_name_for_fd

            try:
                # Before the child attaches its controlling TTY the group is 0
                # (which would misleadingly resolve to kernel_task on macOS).
                if os.tcgetpgrp(self.master) > 0:
                    self._name = Path(get_name_for_fd(self.master) or self.shell).name
                    self._name_checked = time.monotonic()
            except OSError:
                pass
        return self._name

    def get_cwd(self):
        return str(self.cwd)
