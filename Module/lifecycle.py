"""Keep blocking work alive until it has released process-owned resources."""
from __future__ import annotations

import asyncio
import signal


async def run_blocking(function, /, *args, **kwargs):
    def invoke():
        try:
            return function(*args, **kwargs)
        except StopIteration as exc:
            # Python 3.11 cannot transfer StopIteration into an asyncio Future;
            # the worker finishes but its Future (and cancellation join) hangs.
            raise RuntimeError("blocking callable raised StopIteration") from exc

    task = asyncio.create_task(asyncio.to_thread(invoke))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # Cancelling to_thread does not stop its OS thread. Join it before callers
        # close the HTTP session, SQLite connection, or release a decode slot.
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        # Retrieve a failure without replacing the caller's cancellation.
        if not task.cancelled():
            task.exception()
        raise


async def run_until_signal(awaitable):
    loop = asyncio.get_running_loop()
    task = asyncio.create_task(awaitable)
    previous = signal.getsignal(signal.SIGTERM)
    def cancel_once(*_):
        if not task.cancelling():
            loop.call_soon_threadsafe(task.cancel)
    signal.signal(signal.SIGTERM, cancel_once)
    try:
        await task
    except asyncio.CancelledError:
        pass
    finally:
        signal.signal(signal.SIGTERM, previous)
