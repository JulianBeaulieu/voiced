"""Wyoming speech-to-text handler.

Protocol as Home Assistant drives it: ``Transcribe`` (language) followed by
``AudioStart``, ``AudioChunk``..., ``AudioStop``; the reply is one
``Transcript``. ``Describe`` can arrive at any time and gets ``Info``.

An utterance spans several events while the engine holds the inference slot,
so (per ADR 0005) the session runs as its own task: it owns
``async with inference(ctx, Operation.STREAM)``, builds a ``PCMConverter`` from
the ``AudioStart`` format, and consumes chunks from an ``asyncio.Queue`` with
``bounded`` reads so an idle satellite releases the slot. The connection
handler only feeds the queue; the task delivers the transcript on success and
maps its own outcome to a Wyoming ``Error`` on failure (the one place, like
the TTS worker), closing the connection unless the fault was the client's.
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
from voiced.engines.base import EngineError
from voiced.inference import bounded, inference
from voiced.metrics import Operation
from voiced.wyoming.handlers import VoicedEventHandler
from voiced.wyoming.info import stt_info

log = logging.getLogger(__name__)

# One 16-bit sample is two bytes; the utterance cap reuses ``stt.max_upload_mb``
# so a Wyoming stream cannot grow without bound while holding the slot.
_BYTES_PER_SAMPLE = 2


class STTEventHandler(VoicedEventHandler):
    _language: str | None = None
    _queue: asyncio.Queue[AudioChunk | None] | None = None
    _task: asyncio.Task[None] | None = None

    async def handle(self, event: Event) -> bool:
        if Describe.is_type(event.type):
            await self.write_event(stt_info(self.ctx).event())
            return True
        if Transcribe.is_type(event.type):
            self._language = self.ctx.check_language(Transcribe.from_event(event).language)
            return True
        if AudioStart.is_type(event.type):
            start = AudioStart.from_event(event)
            await self._begin(start.rate, start.width, start.channels)
            return True
        if AudioChunk.is_type(event.type):
            chunk = AudioChunk.from_event(event)
            if self._task is None:
                # Some clients skip AudioStart; take the format from the chunk.
                await self._begin(chunk.rate, chunk.width, chunk.channels)
            if self._queue is not None:
                self._queue.put_nowait(chunk)
            return True
        if AudioStop.is_type(event.type):
            return await self._finish()
        log.debug("stt %s: ignoring %s", self.peer, event.type)
        return True

    async def _begin(self, rate: int, width: int, channels: int) -> None:
        await self._cancel_task()  # a new stream without AudioStop drops the old one
        queue: asyncio.Queue[AudioChunk | None] = asyncio.Queue()
        self._queue = queue
        self._task = asyncio.create_task(
            self._session(rate, width, channels, self._language, queue),
            name="wyoming stt session",
        )

    async def _finish(self) -> bool:
        task, queue = self._task, self._queue
        if task is None:
            # AudioStop without audio: answer with an empty transcript.
            await self.write_event(Transcript(text="", language=self._language).event())
            return True
        if queue is not None:
            queue.put_nowait(None)
        await task  # delivers the transcript or reports its own error; never raises
        return True

    async def _session(
        self,
        rate: int,
        width: int,
        channels: int,
        language: str | None,
        queue: asyncio.Queue[AudioChunk | None],
    ) -> None:
        try:
            async with inference(self.ctx, Operation.STREAM):
                self.ctx.check_loaded()
                converter = PCMConverter(rate, width, channels)
                max_samples = self.ctx.settings.stt.max_upload_mb * 1_000_000 // _BYTES_PER_SAMPLE
                session = await self.ctx.stt_engine.stream(language)
                total = 0
                async with session:
                    while (chunk := await bounded(queue.get(), self._idle_timeout_s)) is not None:
                        if (chunk.rate, chunk.width, chunk.channels) != (rate, width, channels):
                            raise UnsupportedAudioFormat("audio format changed mid-stream")
                        samples = converter.convert(chunk.audio)
                        if len(samples):
                            total += len(samples)
                            if total > max_samples:
                                raise EngineError(
                                    f"utterance exceeds {self.ctx.settings.stt.max_upload_mb} MB "
                                    "of audio"
                                )
                            await session.add_audio(samples)
                    transcript = await session.finalize()
            # Delivery is outside the slot, like an HTTP response body.
            await self.write_event(
                Transcript(text=transcript.text, language=transcript.language).event()
            )
        except Exception as exc:
            if not await self.report(exc):
                await self.stop()
        finally:
            if asyncio.current_task() is self._task:
                self._task = None
                self._queue = None

    async def cleanup(self) -> None:
        await self._cancel_task()

    async def _cancel_task(self) -> None:
        task = self._task
        if task is None:
            return
        if not task.done():
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task  # inference() already recorded the outcome; just retrieve it
