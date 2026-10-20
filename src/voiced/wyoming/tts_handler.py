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
agent does not hold the GPU between sentences.

A stream that fails (its ``SynthesizeStart`` refused, its text over
``tts.max_input_chars``, the engine or the slot failing in the worker) sends
one ``Error``. Home Assistant writes the whole stream before it reads, so the
connection stays open and the stream's remaining events, the compatibility
``Synthesize`` included, are read and dropped until ``SynthesizeStop`` closes
it, at most ``server.session_idle_s`` after the failure
(:meth:`VoicedEventHandler.fail_and_drop`).
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

from voiced.engines.base import (
    TTS_SAMPLE_RATE,
    TTS_SAMPLE_WIDTH,
    EngineError,
    PCMChunk,
    Voice,
)
from voiced.inference import inference
from voiced.metrics import Operation
from voiced.text import normalize
from voiced.wyoming.handlers import MalformedEvent, VoicedEventHandler, cancel_task, parse
from voiced.wyoming.info import tts_info
from voiced.wyoming.text import SentenceAccumulator

log = logging.getLogger(__name__)

_AUDIO_FORMAT = {"rate": TTS_SAMPLE_RATE, "width": TTS_SAMPLE_WIDTH, "channels": 1}
_SYNTHESIS_EVENTS = (Synthesize, SynthesizeStart, SynthesizeChunk, SynthesizeStop)


@dataclass
class _Stream:
    voice: Voice
    sentences: asyncio.Queue[str | None] = field(default_factory=asyncio.Queue)
    accumulator: SentenceAccumulator = field(init=False)
    worker: asyncio.Task[None] | None = None
    chars: int = 0

    def __post_init__(self) -> None:
        self.accumulator = SentenceAccumulator(self.voice.language)


def _require_text(text: object, event: Event) -> None:
    if not isinstance(text, str):
        raise MalformedEvent(f"malformed {event.type}: text must be a string")


class TTSEventHandler(VoicedEventHandler):
    _stream: _Stream | None = None
    _audio_started: bool = False

    async def handle(self, event: Event) -> bool:
        if Describe.is_type(event.type):
            await self.write_event((await tts_info(self.ctx)).event())
            return True
        is_synthesis = any(t.is_type(event.type) for t in _SYNTHESIS_EVENTS)
        if is_synthesis and self._dropping_until is not None:
            # None of the failed stream's events may reach the engine or start
            # a new stream.
            worker = self._stream.worker if self._stream is not None else None
            return await self.drop(SynthesizeStop.is_type(event.type), worker)
        if Synthesize.is_type(event.type):
            if self._stream is not None:
                return True  # compatibility copy of the streamed text
            synthesize = parse(Synthesize, event)
            _require_text(synthesize.text, event)
            self._enforce_input_limit(len(synthesize.text))
            await self._synthesize_once(synthesize.text, await self._resolve(synthesize.voice))
            return True
        if SynthesizeStart.is_type(event.type):
            if self._stream is not None:
                await self.cleanup()
            try:
                start = parse(SynthesizeStart, event)
                voice = await self._resolve(start.voice)
            except EngineError as exc:
                await self.fail_and_drop(exc)
                return True
            self._audio_started = False
            self._stream = stream = _Stream(voice=voice)
            stream.worker = asyncio.create_task(self._worker(stream), name="wyoming tts stream")
            return True
        if SynthesizeChunk.is_type(event.type):
            open_stream = self._stream
            if open_stream is None:
                raise EngineError("synthesize-chunk before synthesize-start")
            text = parse(SynthesizeChunk, event).text
            _require_text(text, event)
            open_stream.chars += len(text)
            if open_stream.chars > self.ctx.settings.tts.max_input_chars:
                # One error for the stream: nothing more of it is spoken, and
                # its later chunks and SynthesizeStop are dropped.
                await cancel_task(open_stream.worker)
                open_stream.worker = None
                await self.fail_and_drop(self._input_limit_error())
                return True
            for sentence in open_stream.accumulator.add(text):
                open_stream.sentences.put_nowait(sentence)
            return True
        if SynthesizeStop.is_type(event.type):
            if self._stream is None:
                raise EngineError("synthesize-stop before synthesize-start")
            return await self._finish_stream()
        log.debug("tts %s: ignoring %s", self.peer, event.type)
        return True

    async def _resolve(self, voice: SynthesizeVoice | None) -> Voice:
        """Pick the engine voice. Home Assistant only ever sends ``name``
        (``wyoming/tts.py`` has no language field for synthesis), so an explicit
        name wins and everything else falls back to the configured default.

        Like the HTTP route, the voice must be one the service serves (the
        engine's voices in the configured languages, which ``Describe``
        lists), checked before the engine sees it.
        """
        name = voice.name if voice is not None and voice.name else None
        if name is not None and not isinstance(name, str):
            raise MalformedEvent("voice name must be a string")
        self.ctx.check_loaded()
        return await self.ctx.check_voice(name, listed_by="Describe")

    def _enforce_input_limit(self, chars: int) -> None:
        if chars > self.ctx.settings.tts.max_input_chars:
            raise self._input_limit_error()

    def _input_limit_error(self) -> EngineError:
        return EngineError(
            f"text exceeds max_input_chars ({self.ctx.settings.tts.max_input_chars})"
        )

    async def _ensure_audio_start(self) -> None:
        """Send ``AudioStart`` exactly once per request before any chunk."""
        if not self._audio_started:
            await self.write_event(AudioStart(**_AUDIO_FORMAT).event())
            self._audio_started = True

    async def _end_audio(self) -> None:
        """Close the audio stream: a lone ``AudioStart`` for empty text, then
        ``AudioStop``. Each request resets ``_audio_started`` when it begins."""
        await self._ensure_audio_start()
        await self.write_event(AudioStop().event())

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

    async def _synthesize_once(self, raw_text: str, voice: Voice) -> None:
        text = normalize(raw_text, language=voice.language)
        log.info("tts %s: synthesize %d chars with %s", self.peer, len(text), voice.id)
        self._audio_started = False
        if text:
            await self._speak(text, voice.id)
        await self._end_audio()

    async def _worker(self, stream: _Stream) -> None:
        """Synthesize sentences as they become complete. Owns the stream's
        error policy: one ``except`` reports the outcome, and the connection
        drops the rest of the stream."""
        try:
            while (sentence := await stream.sentences.get()) is not None:
                text = normalize(sentence, language=stream.voice.language)
                if not text:
                    continue
                await self._speak(text, stream.voice.id)
        except Exception as exc:
            # A broken stream always ends, whatever the outcome; report()'s
            # keep-open answer only applies to request/response events.
            await self.fail_and_drop(exc)

    async def _finish_stream(self) -> bool:
        stream = self._stream
        assert stream is not None and stream.worker is not None
        for sentence in stream.accumulator.finish():
            stream.sentences.put_nowait(sentence)
        stream.sentences.put_nowait(None)
        await stream.worker
        self._stream = None
        if self._dropping_until is not None:
            return False  # the worker failed and sent the stream's one Error
        await self._end_audio()
        await self.write_event(SynthesizeStopped().event())
        log.debug("tts %s: stream finished", self.peer)
        return True

    async def cleanup(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            await cancel_task(stream.worker)
