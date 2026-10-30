"""Coordinate native pane resizing with the compact prompt's live composer.

OSC 777 ``tether;1;input;ready|prepared|closed`` is presentation-only metadata.
The host sends Shift-F12 (F24) only while this opt-in binding is advertised.
Erase at the old grid size before the emulator reflows transcript lines, then
redraw at the new size. No prompt text, history, or buffer state is discarded.
"""
from __future__ import annotations

import asyncio
import os
import sys

from prompt_toolkit.application import in_terminal


class NativeResize:
    def __init__(self, app, bindings, *, enabled=None, write=None):
        self.app = app
        self.enabled = (os.environ.get("TETHER_NATIVE_ACTIVITY") == "1" and sys.stdout.isatty()
                        if enabled is None else enabled)
        self.write = write or (lambda data: os.write(1, data))
        self.state = None
        self.preparing = False
        if self.enabled:
            app.after_render += self.ready
            bindings.add("f24", eager=True)(self.prepare)

    def emit(self, state):
        if self.enabled and state != self.state:
            self.state = state
            try:
                self.write(f"\x1b]777;tether;1;input;{state}\x07".encode("ascii"))
            except (OSError, ValueError):
                self.enabled = False

    def ready(self, _app):
        self.emit("ready")

    async def prepare(self, event):
        if self.preparing:
            return
        self.preparing = True
        try:
            previous = self.app.output.get_size()
            async with in_terminal():
                # in_terminal detaches input in cooked mode. Retain raw input
                # so keys typed during this short handshake queue without echo.
                with self.app.input.raw_mode():
                    self.emit("prepared")
                    # Bound the wait if the native host closes or cancels a fit.
                    for _ in range(50):
                        if self.app.output.get_size() != previous:
                            break
                        await asyncio.sleep(.01)
        finally:
            self.preparing = False

    def close(self):
        self.emit("closed")
        self.app.after_render -= self.ready
