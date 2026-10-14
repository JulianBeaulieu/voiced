"""Endpointing: deciding when the speaker has finished.

The engine never sees the voice activity detector (``voiced.engines.base``:
the VAD lives outside the engine). The server feeds every incoming chunk to an
:class:`Endpointer` next to the engine's stream session and calls
``finalize()`` when the endpointer reports end of speech::

    endpointer = create_endpointer(settings.stt.vad_silence_ms)
    async with await engine.stream() as session:
        async for chunk in audio_chunks:
            await session.add_audio(chunk)
            event = endpointer.feed(chunk)
            if event is not None and event.kind == "speech_end":
                transcript = await session.finalize()
                break

Two layers:

* A :class:`SpeechDetector` turns one fixed-size frame (512 samples, 32 ms at
  16 kHz) into a speech probability. :class:`SileroDetector` wraps the Silero
  VAD model through ``pysilero-vad`` (a ggml build, about 1 MB, no torch and no
  onnxruntime). :class:`EnergyDetector` is the dependency-free fallback.
* :class:`SilenceEndpointer` is the state machine on top. Speech starts after
  ``min_speech_ms`` of consecutive speech frames and ends after ``silence_ms``
  of consecutive non-speech frames (``settings.stt.vad_silence_ms``).

Semantic end-of-turn models (smart-turn v3 and the like) plug in as a
:class:`TurnDetector`. When the silence window elapses, the endpointer asks it
whether the utterance so far is a complete turn and otherwise keeps waiting,
re-asking every ``silence_ms`` up to ``max_silence_ms``. No such model ships
with voiced yet; the hook exists so adding one does not touch the server.
"""

from __future__ import annotations

import logging
import math
from collections import deque
from dataclasses import dataclass
from typing import Literal, Protocol

import numpy as np
from numpy.typing import NDArray

from voiced.engines.base import STT_SAMPLE_RATE

__all__ = [
    "FRAME_SAMPLES",
    "EndpointEvent",
    "Endpointer",
    "EnergyDetector",
    "SilenceEndpointer",
    "SileroDetector",
    "SpeechDetector",
    "TurnDetector",
    "VADBackend",
    "create_endpointer",
    "silero_available",
]

log = logging.getLogger(__name__)

FRAME_SAMPLES = 512
"""Silero's fixed analysis window at 16 kHz (32 ms). Both detectors use it."""

EventKind = Literal["speech_start", "speech_end"]
VADBackend = Literal["auto", "silero", "energy"]


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


class TurnDetector(Protocol):
    """Semantic end-of-turn hook. ``utterance`` is the audio since speech started."""

    def is_turn_complete(self, utterance: NDArray[np.float32]) -> bool: ...


class Endpointer(Protocol):
    """What the server depends on. :class:`SilenceEndpointer` is the implementation."""

    def feed(self, samples: NDArray[np.float32]) -> EndpointEvent | None: ...

    def reset(self) -> None: ...


class EnergyDetector:
    """RMS threshold. ``threshold`` is linear amplitude; 0.01 is about -40 dBFS."""

    frame_samples = FRAME_SAMPLES

    def __init__(self, threshold: float = 0.01) -> None:
        if threshold <= 0:
            raise ValueError("threshold must be positive")
        self.threshold = threshold

    def probability(self, frame: NDArray[np.float32]) -> float:
        rms = float(np.sqrt(np.mean(np.square(frame, dtype=np.float32))))
        return 1.0 if rms >= self.threshold else 0.0

    def reset(self) -> None:
        return None


class SileroDetector:
    """Silero VAD through ``pysilero-vad``. Raises ``RuntimeError`` if it is not installed."""

    frame_samples = FRAME_SAMPLES

    def __init__(self) -> None:
        try:
            from pysilero_vad import SileroVoiceActivityDetector
        except ImportError as exc:
            raise RuntimeError(
                "pysilero-vad is not installed; install it or use EnergyDetector"
            ) from exc
        self._vad = SileroVoiceActivityDetector()
        if self._vad.chunk_samples() != FRAME_SAMPLES:
            raise RuntimeError("unexpected Silero window size")

    def probability(self, frame: NDArray[np.float32]) -> float:
        return float(self._vad.process_samples(frame.tolist()))

    def reset(self) -> None:
        self._vad.reset()


def silero_available() -> bool:
    try:
        import pysilero_vad  # noqa: F401
    except ImportError:
        return False
    return True


class SilenceEndpointer:
    """Speech start and end from frame probabilities and a trailing-silence window.

    ``feed`` accepts chunks of any length; samples are carried over between
    calls so chunk boundaries do not have to align with frames. It returns the
    last event raised while processing the chunk, or ``None``. With chunks
    shorter than the silence window (the normal case) at most one event can
    occur per call.
    """

    def __init__(
        self,
        detector: SpeechDetector | None = None,
        *,
        silence_ms: int = 200,
        threshold: float = 0.5,
        min_speech_ms: int = 96,
        max_silence_ms: int | None = None,
        turn_detector: TurnDetector | None = None,
        sample_rate: int = STT_SAMPLE_RATE,
    ) -> None:
        if silence_ms < 0 or min_speech_ms < 0:
            raise ValueError("durations must not be negative")
        if max_silence_ms is not None and max_silence_ms < silence_ms:
            raise ValueError("max_silence_ms must be at least silence_ms")
        self._detector: SpeechDetector = detector if detector is not None else EnergyDetector()
        self._frame = self._detector.frame_samples
        self._rate = sample_rate
        self._threshold = threshold
        self._turn_detector = turn_detector
        self._silence_frames = max(1, self._frames_for(silence_ms))
        self._min_speech_frames = max(1, self._frames_for(min_speech_ms))
        self._max_silence_frames = (
            max(self._silence_frames, self._frames_for(max_silence_ms))
            if max_silence_ms is not None
            else self._silence_frames
        )
        self._remainder: NDArray[np.float32] = np.zeros(0, dtype=np.float32)
        self._frames_seen = 0
        self._in_speech = False
        self._speech_run = 0
        self._silence_run = 0
        self._speech_start_frame = 0
        # Only kept when a turn detector needs the utterance audio.
        self._recent: deque[NDArray[np.float32]] = deque(maxlen=self._min_speech_frames)
        self._utterance: list[NDArray[np.float32]] = []
        self.reset()

    def _frames_for(self, ms: int) -> int:
        return math.ceil(ms * self._rate / 1000 / self._frame)

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
        self._recent.clear()
        self._utterance = []

    def feed(self, samples: NDArray[np.float32]) -> EndpointEvent | None:
        arr = np.asarray(samples, dtype=np.float32)
        if arr.ndim != 1:
            raise ValueError(f"expected mono 1-D samples, got shape {arr.shape}")
        buf = np.concatenate([self._remainder, arr]) if len(self._remainder) else arr
        n_full = len(buf) // self._frame
        event: EndpointEvent | None = None
        for i in range(n_full):
            frame = buf[i * self._frame : (i + 1) * self._frame]
            step = self._step(frame)
            if step is not None:
                event = step
        self._remainder = buf[n_full * self._frame :].copy()
        return event

    def _step(self, frame: NDArray[np.float32]) -> EndpointEvent | None:
        speech = self._detector.probability(frame) >= self._threshold
        self._frames_seen += 1
        if self._turn_detector is not None:
            (self._utterance if self._in_speech else self._recent).append(frame)
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
            if self._turn_detector is not None:
                self._utterance = list(self._recent)
                self._recent.clear()
            return EndpointEvent("speech_start", self.position_s, self._speech_start_s())
        if speech:
            self._silence_run = 0
            return None
        self._silence_run += 1
        if self._silence_run < self._silence_frames:
            return None
        if (
            self._turn_detector is not None
            and self._silence_run < self._max_silence_frames
            and (
                self._silence_run % self._silence_frames != 0
                or not self._turn_detector.is_turn_complete(np.concatenate(self._utterance))
            )
        ):
            return None
        return self._end()

    def _speech_start_s(self) -> float:
        return self._speech_start_frame * self._frame / self._rate

    def _end(self) -> EndpointEvent:
        end_s = (self._frames_seen - self._silence_run) * self._frame / self._rate
        event = EndpointEvent("speech_end", self.position_s, self._speech_start_s(), end_s)
        self._in_speech = False
        self._speech_run = 0
        self._silence_run = 0
        self._utterance = []
        return event


def create_endpointer(
    silence_ms: int = 200,
    *,
    backend: VADBackend = "auto",
    threshold: float | None = None,
    min_speech_ms: int = 96,
    max_silence_ms: int | None = None,
    turn_detector: TurnDetector | None = None,
) -> SilenceEndpointer:
    """Build the default endpointer: Silero when installed, energy otherwise.

    ``silence_ms`` is normally ``settings.stt.vad_silence_ms``. ``threshold``
    defaults to 0.5 for both detectors (the energy detector reports 0 or 1).
    """
    detector: SpeechDetector
    if backend == "energy":
        detector = EnergyDetector()
    else:
        try:
            detector = SileroDetector()
        except RuntimeError:
            if backend == "silero":
                raise
            log.warning("pysilero-vad not installed, falling back to the energy VAD")
            detector = EnergyDetector()
    return SilenceEndpointer(
        detector,
        silence_ms=silence_ms,
        threshold=0.5 if threshold is None else threshold,
        min_speech_ms=min_speech_ms,
        max_silence_ms=max_silence_ms,
        turn_detector=turn_detector,
    )
