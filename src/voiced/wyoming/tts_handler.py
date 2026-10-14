"""Wyoming text-to-speech handler.

Two request shapes, both answered with 24 kHz 16-bit mono audio:

* Plain ``Synthesize`` (text, voice): ``AudioStart``, ``AudioChunk``...,
  ``AudioStop``.
* Streaming (Home Assistant >= 2025.7): ``SynthesizeStart`` (voice),
  ``SynthesizeChunk``... as the conversation agent produces text, then
  ``SynthesizeStop``. Audio starts as soon as the first sentence is complete
  and ends with ``AudioStop`` followed by ``SynthesizeStopped``. Home
  Assistant also sends a plain ``Synthesize`` with the whole message right
  before ``SynthesizeStop`` for servers without streaming support; inside a
  stream it is ignored, otherwise everything would be spoken twice.

Synthesis for a stream runs in a worker task so text keeps being read while
the engine works. Each sentence takes the inference slot on its own
(``async with inference(ctx, Operation.SPEECH)``), so a slow conversation
agent does not hold the GPU between sentences. The worker owns the error
policy for the stream: one ``except`` maps the outcome and closes.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator
from contextlib import aclosing
from dataclasses import dataclass, field
from typing import cast

from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.event import Event
from wyoming.info import Describe
from wyoming.tts import (
    Synthesize,
    SynthesizeChunk,
    SynthesizeStart,
    SynthesizeStop,
    SynthesizeStopped,
    SynthesizeVoice,
)

from voiced.engines.base import TTS_SAMPLE_RATE, TTS_SAMPLE_WIDTH, EngineError, PCMChunk
from voiced.inference import inference
from voiced.metrics import Operation
from voiced.text import normalize
from voiced.wyoming.handlers import VoicedEventHandler
from voiced.wyoming.info import tts_info
from voiced.wyoming.text import SentenceAccumulator

log = logging.getLogger(__name__)

_AUDIO_FORMAT = {"rate": TTS_SAMPLE_RATE, "width": TTS_SAMPLE_WIDTH, "channels": 1}


@dataclass
class _Stream:
    voice: str
    sentences: asyncio.Queue[str | None] = field(default_factory=asyncio.Queue)
    accumulator: SentenceAccumulator = field(default_factory=SentenceAccumulator)
    worker: asyncio.Task[None] | None = None
    chars: int = 0
    failed: bool = False


class TTSEventHandler(VoicedEventHandler):
    _stream: _Stream | None = None
    _audio_started: bool = False

    async def handle(self, event: Event) -> bool:
        if Describe.is_type(event.type):
            await self.write_event((await tts_info(self.ctx)).event())
            return True
        if Synthesize.is_type(event.type):
            if self._stream is not None:
                return True  # compatibility copy of the streamed text
            synthesize = Synthesize.from_event(event)
            self._enforce_input_limit(len(synthesize.text))
            await self._synthesize_once(synthesize.text, self._resolve(synthesize.voice))
            return True
        if SynthesizeStart.is_type(event.type):
            if self._stream is not None:
                await self.cleanup()
            start = SynthesizeStart.from_event(event)
            self._audio_started = False
            self._stream = stream = _Stream(voice=self._resolve(start.voice))
            stream.worker = asyncio.create_task(self._worker(stream), name="wyoming tts stream")
            return True
        if SynthesizeChunk.is_type(event.type):
            open_stream = self._stream
            if open_stream is None:
                raise EngineError("synthesize-chunk before synthesize-start")
            if open_stream.failed:
                return False  # the worker already reported and closed
            text = SynthesizeChunk.from_event(event).text
            open_stream.chars += len(text)
            self._enforce_input_limit(open_stream.chars)
            for sentence in open_stream.accumulator.add(text):
                open_stream.sentences.put_nowait(sentence)
            return True
        if SynthesizeStop.is_type(event.type):
            if self._stream is None:
                raise EngineError("synthesize-stop before synthesize-start")
            return await self._finish_stream()
        log.debug("tts %s: ignoring %s", self.peer, event.type)
        return True

    def _resolve(self, voice: SynthesizeVoice | None) -> str:
        """Pick the engine voice id. Home Assistant only ever sends ``name``
        (``wyoming/tts.py`` has no language field for synthesis), so an explicit
        name wins and everything else falls back to the configured default."""
        if voice is not None and voice.name:
            return voice.name
        return self.ctx.settings.tts.default_voice

    def _enforce_input_limit(self, chars: int) -> None:
        limit = self.ctx.settings.tts.max_input_chars
        if chars > limit:
            raise EngineError(f"text exceeds max_input_chars ({limit})")

    async def _ensure_audio_start(self) -> None:
        """Send ``AudioStart`` exactly once per request before any chunk."""
        if not self._audio_started:
            await self.write_event(AudioStart(**_AUDIO_FORMAT).event())
            self._audio_started = True

    async def _end_audio(self) -> None:
        """Close the audio stream: a lone ``AudioStart`` for empty text, then
        ``AudioStop``. Resets the per-request state."""
        await self._ensure_audio_start()
        await self.write_event(AudioStop().event())
        self._audio_started = False

    async def _speak(self, text: str, voice: str) -> None:
        """Synthesize ``text`` while holding the inference slot, sending
        ``AudioStart`` before the first chunk and an ``AudioChunk`` per chunk."""
        async with inference(self.ctx, Operation.SPEECH):
            self.ctx.check_loaded()
            # base.py guarantees synthesize() is an async generator, so it can
            # be closed early (aclose) when the client disconnects.
            gen = cast(
                "AsyncGenerator[PCMChunk, None]",
                self.ctx.tts_engine.synthesize(text, voice, self.ctx.settings.tts.speed),
            )
            async with aclosing(gen):
                async for chunk in gen:
                    await self._ensure_audio_start()
                    await self.write_event(AudioChunk(**_AUDIO_FORMAT, audio=chunk.data).event())

    async def _synthesize_once(self, raw_text: str, voice: str) -> None:
        text = normalize(raw_text)
        log.info("tts %s: synthesize %d chars with %s", self.peer, len(text), voice)
        self._audio_started = False
        if text:
            await self._speak(text, voice)
        await self._end_audio()

    async def _worker(self, stream: _Stream) -> None:
        """Synthesize sentences as they become complete. Owns the stream's
        error policy: one ``except`` reports the outcome and closes."""
        try:
            while (sentence := await stream.sentences.get()) is not None:
                text = normalize(sentence)
                if not text:
                    continue
                await self._speak(text, stream.voice)
        except Exception as exc:
            stream.failed = True
            await self.report(exc)
            await self.stop()

    async def _finish_stream(self) -> bool:
        stream = self._stream
        assert stream is not None and stream.worker is not None
        for sentence in stream.accumulator.finish():
            stream.sentences.put_nowait(sentence)
        stream.sentences.put_nowait(None)
        await stream.worker
        self._stream = None
        if stream.failed:
            return False
        await self._end_audio()
        await self.write_event(SynthesizeStopped().event())
        log.debug("tts %s: stream finished", self.peer)
        return True

    async def cleanup(self) -> None:
        stream, self._stream = self._stream, None
        if stream is None or stream.worker is None or stream.worker.done():
            return
        stream.worker.cancel()
        try:
            await stream.worker
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise
