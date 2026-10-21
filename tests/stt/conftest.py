"""Shared helpers for the STT tests. The ``model`` marker is handled in ``tests/conftest.py``."""

from __future__ import annotations

import asyncio
import re
import sys
import threading
import time
import types
import wave
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from numpy.typing import NDArray

from voiced.engines.base import STT_SAMPLE_RATE

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def load_fixture(name: str) -> NDArray[np.float32]:
    """Read a 16 kHz mono 16-bit WAV from ``tests/fixtures`` as float32."""
    with wave.open(str(FIXTURES / name)) as wav:
        assert wav.getframerate() == STT_SAMPLE_RATE
        assert wav.getnchannels() == 1
        assert wav.getsampwidth() == 2
        pcm = wav.readframes(wav.getnframes())
    return np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0


def words(text: str) -> list[str]:
    return re.sub(r"[^a-z0-9 ]", " ", text.lower()).split()


def word_accuracy(hypothesis: str, reference: str) -> float:
    """1 minus the word error rate: word edit distance over the reference length,
    case and punctuation insensitive."""
    hyp, ref = words(hypothesis), words(reference)
    row = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        prev, row[0] = row[0], i
        for j, h in enumerate(hyp, 1):
            prev, row[j] = row[j], min(row[j] + 1, row[j - 1] + 1, prev + (r != h))
    return 1.0 - row[-1] / max(len(ref), 1)


@pytest.fixture
def speech() -> NDArray[np.float32]:
    return load_fixture("speech_en.wav")


@pytest.fixture
def speech_with_trailing_silence() -> NDArray[np.float32]:
    return load_fixture("speech_en_trailing_silence.wav")


@contextmanager
def blocking_first_call(model: Any) -> Iterator[Callable[[], Awaitable[None]]]:
    """Hold ``model.generate``'s first call on the model thread until the
    ``with`` block ends; the block awaits the yielded function to know that
    call has started. For tests that cancel a finalize mid-decode."""
    started, release = threading.Event(), threading.Event()
    generate = model.generate

    def held(*args: Any, **kwargs: Any) -> Any:
        if not started.is_set():
            started.set()
            release.wait(30)
        return generate(*args, **kwargs)

    async def wait_started() -> None:
        # Generous: Silero runs over the turn first, slowly on CI runners.
        assert await asyncio.to_thread(started.wait, 30), "the decode never started"

    model.generate = held
    try:
        yield wait_started
    finally:
        release.set()


async def until(condition: Callable[[], bool], timeout_s: float = 5.0) -> None:
    """Yield to the event loop until ``condition`` holds (a cancel reaching a
    task, say); fail after ``timeout_s``."""
    deadline = time.monotonic() + timeout_s
    while not condition():
        assert time.monotonic() < deadline, "condition not met"
        await asyncio.sleep(0)


class TurnRecorder:
    """Stands in for :func:`voiced.gpu.stt_turn`: ``log`` has each enter and
    exit, ``held`` whether a turn is held now."""

    def __init__(self) -> None:
        self.log: list[str] = []
        self.held = False

    @contextmanager
    def __call__(self) -> Iterator[None]:
        self.log.append("enter")
        self.held = True
        try:
            yield
        finally:
            self.held = False
            self.log.append("exit")


def stub_mlx_audio(
    monkeypatch: pytest.MonkeyPatch,
    engine_module: types.ModuleType,
    load_model: Callable[[Path], Any],
    extra: dict[str, types.ModuleType] | None = None,
) -> TurnRecorder:
    """Stand-in ``mlx`` and ``mlx_audio`` modules for an engine that loads its
    model with ``mlx_audio.stt.utils.load_model`` (``extra`` adds modules by
    name), and a :class:`TurnRecorder` as ``engine_module``'s ``stt_turn``."""
    core = types.ModuleType("mlx.core")
    core.clear_cache = lambda: None  # type: ignore[attr-defined]
    mlx = types.ModuleType("mlx")
    mlx.core = core  # type: ignore[attr-defined]
    utils = types.ModuleType("mlx_audio.stt.utils")
    utils.load_model = load_model  # type: ignore[attr-defined]
    modules = {
        "mlx": mlx,
        "mlx.core": core,
        "mlx_audio": types.ModuleType("mlx_audio"),
        "mlx_audio.stt": types.ModuleType("mlx_audio.stt"),
        "mlx_audio.stt.utils": utils,
        **(extra or {}),
    }
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    turns = TurnRecorder()
    monkeypatch.setattr(engine_module, "stt_turn", turns)
    return turns
