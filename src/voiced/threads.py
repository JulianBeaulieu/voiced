"""Run blocking model calls without letting cancellation outrun the GPU.

``asyncio.to_thread`` returns control as soon as the awaiting task is
cancelled, but the thread keeps running. Inside ``queue.slot()`` that releases
the slot while the model is still busy, and the next request starts a second,
concurrent MLX call. :func:`run_blocking` waits for the thread to finish
before letting ``CancelledError`` propagate.

Engines must use it for every blocking model call (instead of
``asyncio.to_thread``) so a slot is never released while GPU work is in flight.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import ParamSpec, TypeVar

P = ParamSpec("P")
T = TypeVar("T")


async def run_blocking(fn: Callable[P, T], /, *args: P.args, **kwargs: P.kwargs) -> T:
    """``await asyncio.to_thread(fn, ...)``, except cancellation waits for ``fn`` to return.

    The caller still sees ``CancelledError``, only later: once the thread is
    done. Whatever ``fn`` returned or raised after the cancel is discarded.
    """
    future = asyncio.ensure_future(asyncio.to_thread(fn, *args, **kwargs))
    try:
        return await asyncio.shield(future)
    except asyncio.CancelledError:
        while not future.done():
            try:
                await asyncio.shield(future)
            except asyncio.CancelledError:
                continue  # cancelled again; keep waiting for the thread
            except Exception:
                break  # fn failed after the cancel; the cancel wins
        if not future.cancelled():
            future.exception()  # mark as retrieved
        raise
