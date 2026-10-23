"""Hand an unloaded model's memory back to the system (ADR 0033)."""

from __future__ import annotations

import gc
from collections.abc import Callable

from voiced.threads import ModelThread


def release_memory() -> None:
    """Collect what an engine just let go of, then return MLX's cache of
    freed GPU buffers to the system. Call it from an engine's ``unload()``
    on the thread that ran the model, after dropping every reference to
    the model. Without MLX (CI, a CPU engine) only the collection runs.

    Measured with Parakeet and Kokoro: once nothing references the model,
    MLX's active memory is zero and the process footprint drops by what
    the load added, less about 0.1 to 0.2 GB of libraries and compiled
    kernels that stay (ADR 0033)."""
    gc.collect()
    try:
        import mlx.core as mx
    except ImportError:
        return
    mx.clear_cache()


async def unload_on(thread: ModelThread, drop: Callable[[], None]) -> None:
    """An engine's ``unload()``: on its model thread, ``drop`` the model's
    state (set every attribute ``load()`` filled back to empty), then
    :func:`release_memory`."""

    def blocking() -> None:
        drop()
        release_memory()

    await thread.run(blocking)
