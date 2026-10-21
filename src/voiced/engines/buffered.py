"""A stream session for engines without live streaming: buffer, then transcribe.

An engine whose model only transcribes whole recordings (Qwen3-ASR, Whisper)
still serves the streaming endpoints with a :class:`BufferedStreamSession`:
``add_audio`` keeps the turn, ``partial()`` is always ``""``, and
``finalize()`` hands the whole turn to the engine once. No partials at all,
rather than partials that each cost as much as a finalize on the engine's one
model thread and delay the real finalize behind them (ADR 0027).

The engine still owns its one-session slot (``_session``, refusing a second
stream or a batch request while one is open, freeing it in ``on_close``);
a third engine of this kind would be the time to move that here too.
"""

from __future__ import annotations

import threading
from collections.abc import Callable

import numpy as np
from numpy.typing import NDArray

from voiced.audio import check_samples
from voiced.engines.base import AudioInput, EngineError, STTStreamSession, Transcript
from voiced.threads import ModelThread

__all__ = ["BufferedStreamSession", "Transcribe"]

Transcribe = Callable[[AudioInput, Callable[[], bool]], Transcript]
"""Blocking, on the engine's model thread: transcribes the whole turn. The
second argument turns true once the finalize is cancelled or the session
closed, so a long turn can stop between pieces."""


class BufferedStreamSession(STTStreamSession):
    """Buffers the turn and transcribes it at :meth:`finalize`; no partials.

    ``transcribe`` runs on ``thread``; ``on_close`` is called with the session
    once it is closed (the engine frees its one session slot).
    """

    def __init__(
        self,
        thread: ModelThread,
        transcribe: Transcribe,
        on_close: Callable[[BufferedStreamSession], None],
    ) -> None:
        self._thread = thread
        self._transcribe = transcribe
        self._on_close = on_close
        self._chunks: list[NDArray[np.float32]] = []
        self._closed = False
        self._stop = threading.Event()  # read on the model thread

    async def add_audio(self, samples: NDArray[np.float32]) -> None:
        self._check_open()
        self._chunks.append(check_samples(samples))

    def partial(self) -> str:
        return ""

    async def finalize(self) -> Transcript:
        self._check_open()
        chunks, self._chunks = self._chunks, []
        audio = AudioInput(np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.float32))
        try:
            return await self._thread.run_stoppable(
                self._stop.set, self._transcribe, audio, self._stop.is_set
            )
        finally:
            await self.close()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._stop.set()
        self._chunks = []
        self._on_close(self)

    def _check_open(self) -> None:
        if self._closed:
            raise EngineError("stream session is closed")
