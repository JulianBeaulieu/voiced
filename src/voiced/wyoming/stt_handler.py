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
at 16 kHz (:class:`voiced.inference.UtteranceBudget`, about 26 minutes at the default 50 MB),
and the whole session is bounded by that duration plus ``server.session_idle_s``,
so a client trickling tiny chunks cannot hold the slot forever. One chunk may
carry at most :data:`MAX_EVENT_BYTES`, so the queue holds at most
``_QUEUE_CHUNKS * MAX_EVENT_BYTES`` per connection; a client that fills it
while the slot is busy gets ``server_busy`` once ``session_idle_s`` runs out.
"""

from __future__ import annotations

import asyncio
import logging

from wyoming.asr import Transcribe, Transcript
from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.event import Event
from wyoming.info import Describe

from voiced.audio import PCMConverter, UnsupportedAudioFormat
from voiced.inference import ClientTimeout, UtteranceBudget, bounded, inference
from voiced.metrics import Operation
from voiced.queue import QueueFull
from voiced.wyoming.handlers import VoicedEventHandler, cancel_task, parse
from voiced.wyoming.info import stt_info

log = logging.getLogger(__name__)

# Chunks buffered between the connection and the session task. Home Assistant
# sends about 16 chunks per second, so this is a few seconds of audio; beyond
# it the handler stops reading and TCP pushes back on the client.
_QUEUE_CHUNKS = 64


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
            self._language = self.ctx.check_language(parse(Transcribe, event).language)
            return True
        is_audio = any(t.is_type(event.type) for t in (AudioStart, AudioChunk, AudioStop))
        if is_audio and self._task is not None and self._task.done():
            # The session failed and reported; its stop() lets events already
            # buffered through. None of them may reach or restart a session.
            return False
        if AudioStart.is_type(event.type):
            start = parse(AudioStart, event)
            await self._begin(start.rate, start.width, start.channels)
            return True
        if AudioChunk.is_type(event.type):
            chunk = parse(AudioChunk, event)
            if self._task is None:
                # Some clients skip AudioStart; take the format from the chunk.
                await self._begin(chunk.rate, chunk.width, chunk.channels)
            await self._feed(chunk)
            return True
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

    async def _feed(self, item: AudioChunk | None) -> None:
        """Queue ``item`` for the session, waiting while the queue is full.

        The queue only stays full while the session cannot drain it, which
        means it is still waiting for the slot (or the engine is slow): the
        server is the slow side, so running out of time is ``QueueFull``
        (``server_busy``), not a client timeout.
        """
        assert self._queue is not None
        try:
            await bounded(self._queue.put(item), self._idle_timeout_s)
        except ClientTimeout:
            raise QueueFull(self.ctx.queue.capacity) from None

    async def _finish(self) -> bool:
        task = self._task
        if task is None:
            # AudioStop without audio: answer with an empty transcript.
            await self.write_event(Transcript(text="", language=self._language).event())
            return True
        await self._feed(None)
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
                session = await self.ctx.stt_engine.stream(language)
                fmt = (rate, width, channels)
                async with session:
                    async with UtteranceBudget(self.ctx.settings) as budget:
                        while (
                            chunk := await bounded(queue.get(), self._idle_timeout_s)
                        ) is not None:
                            if (chunk.rate, chunk.width, chunk.channels) != fmt:
                                raise UnsupportedAudioFormat("audio format changed mid-stream")
                            samples = converter.convert(chunk.audio)
                            budget.add(len(samples))
                            if len(samples):
                                await session.add_audio(samples)
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
            await self.report(exc)
            await self.stop()
            return False

    async def cleanup(self) -> None:
        task, self._task, self._queue = self._task, None, None
        await cancel_task(task)
