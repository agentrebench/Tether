"""Bounds and cancellation for the native composer's resize handshake."""
import asyncio
from contextlib import asynccontextmanager, nullcontext
from types import SimpleNamespace
from unittest.mock import patch

from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.utils import Event

from tether.ui.native_resize import NativeResize


def bridge(enabled=True):
    size = [SimpleNamespace(columns=80, rows=24)]
    app = SimpleNamespace(output=SimpleNamespace(get_size=lambda: size[0]),
                          input=SimpleNamespace(raw_mode=nullcontext))
    app.after_render = Event(app)
    writes = []
    bindings = KeyBindings()
    resize = NativeResize(app, bindings, enabled=enabled, write=writes.append)
    return resize, app, bindings, writes, size


@asynccontextmanager
async def suspended():
    yield


def test_opt_in_and_lifecycle():
    resize, app, bindings, writes, _ = bridge(False)
    app.after_render.fire()
    resize.close()
    assert not writes and not bindings.bindings
    resize, app, bindings, writes, _ = bridge()
    app.after_render.fire()
    app.after_render.fire()
    resize.close()
    app.after_render.fire()
    assert writes == [b'\x1b]777;tether;1;input;ready\x07', b'\x1b]777;tether;1;input;closed\x07']
    assert len(bindings.bindings) == 1


def test_resize_wait_is_bounded_without_a_host_response():
    async def run():
        resize, _, _, writes, _ = bridge()
        with patch('tether.ui.native_resize.in_terminal', suspended):
            await asyncio.wait_for(resize.prepare(None), timeout=1.5)
        assert not resize.preparing
        assert writes == [b'\x1b]777;tether;1;input;prepared\x07']
    asyncio.run(run())


def test_duplicate_prepare_and_cancellation():
    async def run():
        resize, _, _, writes, _ = bridge()
        with patch('tether.ui.native_resize.in_terminal', suspended):
            task = asyncio.create_task(resize.prepare(None))
            await asyncio.sleep(.02)
            await resize.prepare(None)
            assert len(writes) == 1
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        assert not resize.preparing
    asyncio.run(run())


def test_prepare_finishes_when_pty_size_changes():
    async def run():
        resize, _, _, _, size = bridge()
        with patch('tether.ui.native_resize.in_terminal', suspended):
            task = asyncio.create_task(resize.prepare(None))
            await asyncio.sleep(.02)
            size[0] = SimpleNamespace(columns=100, rows=30)
            await asyncio.wait_for(task, timeout=.2)
        assert not resize.preparing
    asyncio.run(run())
