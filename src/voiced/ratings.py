"""Quality and speed ratings, 1 to 5, from benchmark numbers (ADR 0024).

The limits are fixed, so the same numbers give everyone the same rating:
``scripts/bench_engine.py`` rates each result with them and generates the
limits table in docs/benchmarks.md from :data:`SCALES`, and the model
catalogue's ``quality`` and ``speed`` are checked against them. Standard
library only.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class Scale:
    """One measured number to a rating of 1 to 5. ``bounds`` are the limits
    for 5, 4, 3 and 2, best first; a value past the last is 1. A value on a
    bound gets the better rating."""

    kind: Literal["stt", "tts"]
    axis: Literal["quality", "speed"]
    measure: str
    unit: str
    bounds: tuple[float, float, float, float]
    higher_is_better: bool = False

    def rate(self, value: float) -> int:
        if not math.isfinite(value):
            raise ValueError(f"{self.measure}: {value} is not a measurement")
        for rating, bound in zip((5, 4, 3, 2), self.bounds, strict=True):
            if (value >= bound) if self.higher_is_better else (value <= bound):
                return rating
        return 1


STT_WER = Scale("stt", "quality", "word error rate on the clip set", "%", (3, 5, 8, 12))
STT_FINALIZE = Scale(
    "stt", "speed", "finalize after 200 ms of silence, worst", " ms", (50, 150, 300, 600)
)
STT_RTF = Scale("stt", "speed", "batch real-time factor", "", (0.05, 0.1, 0.2, 0.5))
TTS_UTMOS = Scale(
    "tts", "quality", "UTMOS naturalness", "", (4.3, 4.0, 3.6, 3.0), higher_is_better=True
)
TTS_FIRST_AUDIO = Scale("tts", "speed", "first audio, median", " ms", (100, 200, 400, 800))
TTS_RTF = Scale("tts", "speed", "real-time factor", "", (0.1, 0.25, 0.5, 1.0))

SCALES: tuple[Scale, ...] = (STT_WER, STT_FINALIZE, STT_RTF, TTS_UTMOS, TTS_FIRST_AUDIO, TTS_RTF)


@dataclass(frozen=True)
class Rating:
    quality: int | None  # None: not measured
    speed: int


def rate_stt(wer_percent: float, finalize_worst_ms: float, batch_rtf: float) -> Rating:
    """Speed is the lower of the finalize and the throughput rating: the
    slower path a user waits on."""
    speed = min(STT_FINALIZE.rate(finalize_worst_ms), STT_RTF.rate(batch_rtf))
    return Rating(STT_WER.rate(wer_percent), speed)


def rate_tts(utmos: float | None, first_audio_median_ms: float, rtf: float) -> Rating:
    """Speed is the lower of the first-audio and the throughput rating: an
    engine slower than real time stalls playback whatever its first audio."""
    speed = min(TTS_FIRST_AUDIO.rate(first_audio_median_ms), TTS_RTF.rate(rtf))
    return Rating(None if utmos is None else TTS_UTMOS.rate(utmos), speed)
