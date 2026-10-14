"""Wyoming speech-to-text handler.

Protocol as Home Assistant drives it: ``Transcribe`` (language) followed by
``AudioStart``, ``AudioChunk``..., ``AudioStop``; the reply is one
``Transcript``. ``Describe`` can arrive at any time and gets ``Info``.

An utterance spans several events while the engine holds the inference slot,
so (ADR 0005) the session runs as its own task: it owns
``async with inference(ctx, Operation.STREAM)``, builds a ``PCMConverter`` from
the ``AudioStart`` format, and consumes chunks from a bounded ``asyncio.Queue``
with ``bounded`` reads, so an idle satellite releases the slot. The connection
handler only feeds the queue (with backpressure) and never feeds a session that
has ended. The task delivers the transcript on success; on any failure it
reports the outcome and closes the connection, like the TTS worker.

Limits: an utterance may carry at most ``stt.max_upload_mb`` of 16-bit audio
at 16 kHz (:func:`max_utterance_s`, about 26 minutes at the default 50 MB), and
the whole session is bounded by that duration plus ``server.session_idle_s``,
so a client trickling tiny chunks cannot hold the slot forever.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from wyoming.asr import Transcribe, Transcript
from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.event import Event
from wyoming.info import Describe

from voiced.audio import PCMConverter, UnsupportedAudioFormat
from voiced.config import Settings
from voiced.engines.base import STT_SAMPLE_RATE, EngineError
from voiced.inference import bounded, inference
from voiced.metrics import Operation
from voiced.wyoming.handlers import VoicedEventHandler, cancel_task
from voiced.wyoming.info import stt_info

log = logging.getLogger(__name__)

# Chunks buffered between the connection and the session task. Home Assistant
# sends about 16 chunks per second, so this is a few seconds of audio; beyond
# it the handler stops reading and TCP pushes back on the client.
_QUEUE_CHUNKS = 64
_BYTES_PER_SAMPLE = 2  # the cap reads stt.max_upload_mb as 16-bit samples


def max_utterance_s(settings: Settings) -> float:
    """Longest utterance a Wyoming client may stream, in seconds."""
    return settings.stt.max_upload_mb * 1_000_000 / _BYTES_PER_SAMPLE / STT_SAMPLE_RATE


class STTEventHandler(VoicedEventHandler):
    _language: str | None = None
    _queue: asyncio.Queue[AudioChunk | None] | None = None
    _task: asyncio.Task[bool] | None = None

    async def handle(self, event: Event) -> bool:
        if Describe.is_type(event.type):
            await self.write_event(stt_info(self.ctx).event())
            return True
        if Transcribe.is_type(event.type):
            self._language = None  # a rejected hint must not leave the old one in use
            self._language = self.ctx.check_language(Transcribe.from_event(event).language)
            return True
        if AudioStart.is_type(event.type) or AudioChunk.is_type(event.type):
            if self._task is not None and self._task.done():
                return False  # the session failed, reported, and is closing the connection
        if AudioStart.is_type(event.type):
            start = AudioStart.from_event(event)
            await self._begin(start.rate, start.width, start.channels)
            return True
        if AudioChunk.is_type(event.type):
            chunk = AudioChunk.from_event(event)
            if self._task is None:
                # Some clients skip AudioStart; take the format from the chunk.
                await self._begin(chunk.rate, chunk.width, chunk.channels)
            return await self._feed(chunk)
        if AudioStop.is_type(event.type):
            return await self._finish()
        log.debug("stt %s: ignoring %s", self.peer, event.type)
        return True

    async def _begin(self, rate: int, width: int, channels: int) -> None:
        await self.cleanup()  # AudioStart without AudioStop drops the previous stream
        queue: asyncio.Queue[AudioChunk | None] = asyncio.Queue(maxsize=_QUEUE_CHUNKS)
        self._queue = queue
        self._task = asyncio.create_task(
            self._session(rate, width, channels, self._language, queue),
            name="wyoming stt session",
        )

    async def _feed(self, item: AudioChunk | None) -> bool:
        """Queue ``item`` for the session; ``False`` if the session has ended."""
        task, queue = self._task, self._queue
        assert task is not None and queue is not None
        if task.done():
            return False
        await bounded(queue.put(item), self._idle_timeout_s)
        return True

    async def _finish(self) -> bool:
        task = self._task
        if task is None:
            # AudioStop without audio: answer with an empty transcript.
            await self.write_event(Transcript(text="", language=self._language).event())
            return True
        if not await self._feed(None):
            return False
        ok = await task  # the task delivers the transcript or reports its failure
        self._task = self._queue = None
        return ok

    async def _session(
        self,
        rate: int,
        width: int,
        channels: int,
        language: str | None,
        queue: asyncio.Queue[AudioChunk | None],
    ) -> bool:
        """One utterance. Returns ``True`` once the transcript is delivered; on
        failure reports the outcome, closes the connection and returns ``False``."""
        try:
            async with inference(self.ctx, Operation.STREAM):
                self.ctx.check_loaded()
                converter = PCMConverter(rate, width, channels)
                limit_s = max_utterance_s(self.ctx.settings)
                max_samples = int(limit_s * STT_SAMPLE_RATE)
                session = await self.ctx.stt_engine.stream(language)
                fmt = (rate, width, channels)
                total = 0
                async with session:
                    deadline = asyncio.timeout(limit_s + self._idle_timeout_s)
                    try:
                        async with deadline:
                            while (
                                chunk := await bounded(queue.get(), self._idle_timeout_s)
                            ) is not None:
                                if (chunk.rate, chunk.width, chunk.channels) != fmt:
                                    raise UnsupportedAudioFormat("audio format changed mid-stream")
                                samples = converter.convert(chunk.audio)
                                total += len(samples)
                                if total > max_samples:
                                    raise EngineError(f"utterance longer than {limit_s:.0f} s")
                                if len(samples):
                                    await session.add_audio(samples)
                    except TimeoutError:
                        if not deadline.expired():
                            raise
                        raise EngineError(f"utterance longer than {limit_s:.0f} s") from None
                    transcript = await session.finalize()
            # Delivery is outside the slot, like an HTTP response body.
            await self.write_event(
                Transcript(text=transcript.text, language=transcript.language).event()
            )
            return True
        except Exception as exc:
            # A failed session never restarts on the same connection: a client
            # that keeps streaming after an error must not get a transcript of
            # the leftover audio or a fresh size budget.
            _drain(queue)  # unblock a handler waiting on a full queue
            await self.report(exc)
            await self.stop()
            return False

    async def cleanup(self) -> None:
        task, self._task, self._queue = self._task, None, None
        await cancel_task(task)


def _drain(queue: asyncio.Queue[AudioChunk | None]) -> None:
    with contextlib.suppress(asyncio.QueueEmpty):
        while True:
            queue.get_nowait()
