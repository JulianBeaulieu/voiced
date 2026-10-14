"""Endpointing: deciding when the speaker has finished.

The engine never sees the voice activity detector (``voiced.engines.base``:
the VAD lives outside the engine). The server feeds every incoming chunk to
the :class:`Endpointer` next to the engine's stream session and calls
``finalize()`` when it reports end of speech::

    endpointer = create_endpointer(settings.stt)
    async with await engine.stream() as session:
        async for chunk in audio_chunks:
            await session.add_audio(chunk)
            for event in endpointer.feed(chunk):
                if event.kind == "speech_end":
                    transcript = await session.finalize()

Two layers:

* A :class:`SpeechDetector` turns one fixed-size frame (512 samples, 32 ms at
  16 kHz) into a speech probability. :class:`SileroDetector` wraps the Silero
  VAD model through ``pysilero-vad`` (a ggml build, about 1 MB, no torch and no
  onnxruntime). ``voiced.testing.EnergyDetector`` is the deterministic stand-in
  for tests with synthetic signals, which Silero rightly rejects as non-speech.
* :class:`Endpointer` is the state machine on top. Speech starts after
  ``min_speech_ms`` of consecutive speech frames and ends after ``silence_ms``
  of consecutive non-speech frames (``settings.stt.vad_silence_ms``). Both are
  rounded to whole 32 ms frames.

Semantic end-of-turn models (smart-turn and the like) would sit in the same
place: a detector that, once the silence window has elapsed, is asked whether
the utterance is complete before ``speech_end`` is raised. Nothing ships with
voiced yet; see docs/decisions/0004-stt-engine-parakeet.md.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

import numpy as np
from numpy.typing import NDArray
from pysilero_vad import SileroVoiceActivityDetector

from voiced.config import STTConfig, load_settings
from voiced.engines.base import STT_SAMPLE_RATE

__all__ = [
    "FRAME_SAMPLES",
    "EndpointEvent",
    "Endpointer",
    "SileroDetector",
    "SpeechDetector",
    "create_endpointer",
]

FRAME_SAMPLES = 512
"""Silero's fixed analysis window at 16 kHz (32 ms)."""

EventKind = Literal["speech_start", "speech_end"]


@dataclass(frozen=True, slots=True)
class EndpointEvent:
    """A change in speech state.

    ``offset_s`` is the stream position (seconds of audio fed since the last
    ``reset``) at which the event was raised. ``speech_start_s`` is where the
    current utterance began. For ``speech_end``, ``speech_end_s`` is the last
    speech heard, that is ``offset_s`` minus the silence window that had to
    elapse before the decision; it is ``None`` for ``speech_start``.
    """

    kind: EventKind
    offset_s: float
    speech_start_s: float
    speech_end_s: float | None = None


class SpeechDetector(Protocol):
    """Per-frame speech probability. Frames are ``frame_samples`` float32 samples."""

    frame_samples: int

    def probability(self, frame: NDArray[np.float32]) -> float: ...

    def reset(self) -> None: ...


class SileroDetector:
    """Silero VAD v6 through ``pysilero-vad``."""

    frame_samples = FRAME_SAMPLES

    def __init__(self) -> None:
        self._vad = SileroVoiceActivityDetector()
        if self._vad.chunk_samples() != FRAME_SAMPLES:
            raise RuntimeError("unexpected Silero window size")

    def probability(self, frame: NDArray[np.float32]) -> float:
        return float(self._vad.process_samples(frame.tolist()))

    def reset(self) -> None:
        self._vad.reset()


class Endpointer:
    """Speech start and end from frame probabilities and a trailing-silence window.

    ``feed`` accepts chunks of any length; samples are carried over between
    calls so chunk boundaries do not have to align with frames. It returns
    every event raised while processing the chunk, in order, so a short pause
    and a new utterance inside one chunk are both reported.
    """

    def __init__(
        self,
        detector: SpeechDetector | None = None,
        *,
        silence_ms: int,
        threshold: float = 0.5,
        min_speech_ms: int = 96,
        sample_rate: int = STT_SAMPLE_RATE,
    ) -> None:
        if silence_ms < 0 or min_speech_ms < 0:
            raise ValueError("durations must not be negative")
        self._detector: SpeechDetector = detector if detector is not None else SileroDetector()
        self._frame = self._detector.frame_samples
        self._rate = sample_rate
        self._threshold = threshold
        self._silence_frames = self._frames_for(silence_ms)
        self._min_speech_frames = self._frames_for(min_speech_ms)
        self._remainder: NDArray[np.float32] = np.zeros(0, dtype=np.float32)
        self._frames_seen = 0
        self._in_speech = False
        self._speech_run = 0
        self._silence_run = 0
        self._speech_start_frame = 0

    def _frames_for(self, ms: int) -> int:
        return max(1, round(ms * self._rate / 1000 / self._frame))

    @property
    def silence_s(self) -> float:
        """The trailing-silence window actually applied, after frame rounding."""
        return self._silence_frames * self._frame / self._rate

    @property
    def in_speech(self) -> bool:
        return self._in_speech

    @property
    def position_s(self) -> float:
        """Seconds of audio consumed in whole frames."""
        return self._frames_seen * self._frame / self._rate

    def reset(self) -> None:
        self._detector.reset()
        self._remainder = np.zeros(0, dtype=np.float32)
        self._frames_seen = 0
        self._in_speech = False
        self._speech_run = 0
        self._silence_run = 0
        self._speech_start_frame = 0

    def feed(self, samples: NDArray[np.float32]) -> list[EndpointEvent]:
        arr = np.asarray(samples, dtype=np.float32)
        if arr.ndim != 1:
            raise ValueError(f"expected mono 1-D samples, got shape {arr.shape}")
        buf = np.concatenate([self._remainder, arr]) if len(self._remainder) else arr
        n_full = len(buf) // self._frame
        events: list[EndpointEvent] = []
        for i in range(n_full):
            event = self._step(buf[i * self._frame : (i + 1) * self._frame])
            if event is not None:
                events.append(event)
        self._remainder = buf[n_full * self._frame :].copy()
        return events

    def _step(self, frame: NDArray[np.float32]) -> EndpointEvent | None:
        speech = self._detector.probability(frame) >= self._threshold
        self._frames_seen += 1
        if not self._in_speech:
            if not speech:
                self._speech_run = 0
                return None
            self._speech_run += 1
            if self._speech_run < self._min_speech_frames:
                return None
            self._in_speech = True
            self._silence_run = 0
            self._speech_start_frame = self._frames_seen - self._speech_run
            return EndpointEvent("speech_start", self.position_s, self._speech_start_s())
        if speech:
            self._silence_run = 0
            return None
        self._silence_run += 1
        if self._silence_run < self._silence_frames:
            return None
        end_s = (self._frames_seen - self._silence_run) * self._frame / self._rate
        event = EndpointEvent("speech_end", self.position_s, self._speech_start_s(), end_s)
        self._in_speech = False
        self._speech_run = 0
        self._silence_run = 0
        return event

    def _speech_start_s(self) -> float:
        return self._speech_start_frame * self._frame / self._rate


def create_endpointer(
    stt: STTConfig | None = None, *, detector: SpeechDetector | None = None
) -> Endpointer:
    """The endpointer the server uses: Silero with ``stt.vad_silence_ms``.

    ``stt`` defaults to the loaded settings' STT section. ``detector`` is for
    tests.
    """
    config = stt if stt is not None else load_settings().stt
    return Endpointer(detector, silence_ms=config.vad_silence_ms)
