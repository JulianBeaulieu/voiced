"""BufferedStreamSession, with a stand-in transcribe on a real ModelThread."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable

import numpy as np
import pytest

from voiced.engines.base import AudioInput, EngineError, Transcript
from voiced.engines.buffered import BufferedStreamSession
from voiced.threads import ModelThread

SR = 16_000


class _Engine:
    """Records each turn transcribed and each close; ``fail`` raises in transcribe."""

    def __init__(self) -> None:
        self.thread = ModelThread("buffered-test")
        self.turns: list[AudioInput] = []
        self.closed: list[BufferedStreamSession] = []
        self.fail = False

    def transcribe(self, audio: AudioInput, stop: Callable[[], bool]) -> Transcript:
        self.turns.append(audio)
        if self.fail:
            raise RuntimeError("decoder fell over")
        return Transcript(text="heard", language="en", duration_s=audio.duration_s)

    def session(self) -> BufferedStreamSession:
        return BufferedStreamSession(self.thread, self.transcribe, self.closed.append)


async def test_the_turn_is_transcribed_once_at_finalize() -> None:
    engine = _Engine()
    session = engine.session()
    for _ in range(3):
        await session.add_audio(np.full(SR // 2, 0.1, np.float32))
        assert session.partial() == ""
    assert engine.turns == []
    transcript = await session.finalize()
    assert (transcript.text, transcript.duration_s) == ("heard", 1.5)
    ((audio),) = engine.turns
    assert (len(audio.samples), audio.sample_rate) == (3 * SR // 2, SR)
    assert engine.closed == [session]  # closed by finalize
    with pytest.raises(EngineError, match="closed"):
        await session.add_audio(np.zeros(SR, np.float32))
    with pytest.raises(EngineError, match="closed"):
        await session.finalize()
    await session.close()
    assert engine.closed == [session]  # once


async def test_an_empty_turn_is_an_empty_input() -> None:
    engine = _Engine()
    transcript = await engine.session().finalize()
    assert transcript.duration_s == 0.0
    assert len(engine.turns[0].samples) == 0


async def test_bad_samples_are_refused_at_add_audio() -> None:
    session = _Engine().session()
    with pytest.raises(EngineError):
        await session.add_audio(np.full(SR, np.nan, np.float32))


async def test_closing_without_finalize_discards_the_turn() -> None:
    engine = _Engine()
    async with engine.session() as session:
        await session.add_audio(np.zeros(SR, np.float32))
    assert engine.turns == []
    assert engine.closed == [session]


async def test_a_failed_finalize_closes_the_session() -> None:
    engine = _Engine()
    engine.fail = True
    session = engine.session()
    with pytest.raises(RuntimeError, match="fell over"):
        await session.finalize()
    assert engine.closed == [session]


async def test_a_cancelled_finalize_asks_the_transcription_to_stop() -> None:
    """The cancel waits for the model thread, but ``stop`` turns true at once,
    so a transcription that checks it returns early."""
    engine = _Engine()
    started = threading.Event()
    stopped: list[bool] = []

    def slow(audio: AudioInput, stop: Callable[[], bool]) -> Transcript:
        started.set()
        for _ in range(500):  # up to 5 s
            if stop():
                break
            threading.Event().wait(0.01)
        stopped.append(stop())
        return Transcript(text="", language=None, duration_s=0.0)

    session = BufferedStreamSession(engine.thread, slow, engine.closed.append)
    task = asyncio.create_task(session.finalize())
    await asyncio.to_thread(started.wait, 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stopped == [True]
    assert engine.closed == [session]
