"""Deterministic fake engines.

They honour the contracts in ``voiced.engines.base`` without any model so the
API, Wyoming and CLI layers can be tested on any machine (including CI on a
GitHub runner). Both are registered under the engine name ``fake``.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import numpy as np
from numpy.typing import NDArray

from voiced.engines.base import (
    STT_SAMPLE_RATE,
    TTS_SAMPLE_RATE,
    AudioInput,
    EngineError,
    EngineNotLoaded,
    PCMChunk,
    Segment,
    STTEngine,
    STTStreamSession,
    Transcript,
    TTSEngine,
    UnknownVoice,
    UnsupportedLanguage,
    Voice,
)

FAKE_VOICES = [
    Voice(id="af_heart", name="Heart", language="en-US", gender="female"),
    Voice(id="am_adam", name="Adam", language="en-US", gender="male"),
    Voice(id="bf_emma", name="Emma", language="en-GB", gender="female"),
]


class FakeSTTStreamSession(STTStreamSession):
    """Reports how much audio it has seen; ``finalize`` returns a fixed text."""

    def __init__(self, engine: FakeSTTEngine, language: str | None) -> None:
        self._engine = engine
        self._language = language
        self._samples = 0
        self._closed = False

    async def add_audio(self, samples: NDArray[np.float32]) -> None:
        self._check_open()
        self._samples += len(samples)

    def partial(self) -> str:
        if self._samples == 0:
            return ""
        return self._engine.partial_text

    async def finalize(self) -> Transcript:
        self._check_open()
        self._closed = True
        await asyncio.sleep(self._engine.latency_s)
        duration = self._samples / STT_SAMPLE_RATE
        if self._samples == 0:
            return Transcript(text="", language=self._language, duration_s=0.0)
        return Transcript(
            text=self._engine.transcript_text,
            language=self._language or "en",
            duration_s=duration,
            segments=(Segment(0.0, duration, self._engine.transcript_text),),
        )

    async def close(self) -> None:
        self._closed = True

    def _check_open(self) -> None:
        if self._closed:
            raise EngineError("stream session is closed")


class FakeSTTEngine(STTEngine):
    name = "fake"
    model_id = "fake/stt"
    languages = ["en"]
    license = "MIT"

    def __init__(
        self,
        model_id: str | None = None,
        *,
        transcript_text: str = "hello world",
        partial_text: str = "hello",
        latency_s: float = 0.0,
    ) -> None:
        super().__init__(model_id)
        self.transcript_text = transcript_text
        self.partial_text = partial_text
        self.latency_s = latency_s
        self.loaded = False
        self.calls = 0

    async def load(self) -> None:
        await asyncio.sleep(self.latency_s)
        self.loaded = True

    async def transcribe(self, audio: AudioInput, language: str | None = None) -> Transcript:
        self._check()
        if language is not None and language not in self.languages:
            raise UnsupportedLanguage(language)
        self.calls += 1
        await asyncio.sleep(self.latency_s)
        if len(audio.samples) == 0 or not np.any(audio.samples):
            return Transcript(text="", language=language, duration_s=audio.duration_s)
        return Transcript(
            text=self.transcript_text,
            language=language or "en",
            duration_s=audio.duration_s,
            segments=(Segment(0.0, audio.duration_s, self.transcript_text),),
        )

    async def stream(self, language: str | None = None) -> STTStreamSession:
        self._check()
        if language is not None and language not in self.languages:
            raise UnsupportedLanguage(language)
        self.calls += 1
        return FakeSTTStreamSession(self, language)

    def _check(self) -> None:
        if not self.loaded:
            raise EngineNotLoaded(self.name)


class FakeTTSEngine(TTSEngine):
    """Yields one chunk of silence per sentence, 100 ms per 10 characters."""

    name = "fake"
    model_id = "fake/tts"
    languages = ["en"]
    license = "MIT"

    @classmethod
    def catalogue_voices(cls) -> list[Voice]:
        return list(FAKE_VOICES)

    def __init__(self, model_id: str | None = None, *, latency_s: float = 0.0) -> None:
        super().__init__(model_id)
        self.latency_s = latency_s
        self.loaded = False
        self.calls = 0

    async def load(self) -> None:
        await asyncio.sleep(self.latency_s)
        self.loaded = True

    async def voices(self) -> list[Voice]:
        self._check()
        return list(FAKE_VOICES)

    async def synthesize(
        self, text: str, voice: str, speed: float = 1.0
    ) -> AsyncIterator[PCMChunk]:
        self._check()
        if voice not in {v.id for v in FAKE_VOICES}:
            raise UnknownVoice(voice)
        if speed <= 0:
            raise EngineError("speed must be positive")
        self.calls += 1
        for sentence in _sentences(text):
            await asyncio.sleep(self.latency_s)
            seconds = max(len(sentence), 1) / 10 * 0.1 / speed
            samples = int(seconds * TTS_SAMPLE_RATE)
            yield PCMChunk(data=bytes(samples * 2), sample_rate=TTS_SAMPLE_RATE)

    def _check(self) -> None:
        if not self.loaded:
            raise EngineNotLoaded(self.name)


def _sentences(text: str) -> list[str]:
    out, buf = [], ""
    for ch in text:
        buf += ch
        if ch in ".!?":
            out.append(buf.strip())
            buf = ""
    if buf.strip():
        out.append(buf.strip())
    return out
