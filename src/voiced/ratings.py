"""Quality and speed ratings, 1 to 5, from benchmark numbers (ADR 0024).

The limits are fixed, so the same numbers give everyone the same rating:
``scripts/bench_engine.py`` rates each result with them and generates the
limits table in docs/benchmarks.md from :data:`SCALES`, and the model
catalogue's ``quality`` and ``speed`` are checked against them. Each scale
names the number of a result's ``metrics`` it rates, so :func:`rated` reads
the same numbers for both. Standard library only.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Literal


@dataclass(frozen=True)
class Scale:
    """One measured number to a rating of 1 to 5. ``bounds`` are the limits
    for 5, 4, 3 and 2, best first; a value past the last is 1. A value on a
    bound gets the better rating. ``metric`` is the field of a benchmark
    result's ``metrics`` it rates, dotted into nested objects."""

    kind: Literal["stt", "tts"]
    axis: Literal["quality", "speed"]
    metric: str
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


STT_WER = Scale(
    "stt", "quality", "wer_percent", "word error rate on the clip set", "%", (3, 5, 8, 12)
)
STT_FINALIZE = Scale(
    "stt",
    "speed",
    "finalize_after_silence.worst_ms",
    "finalize after 200 ms of silence, worst",
    " ms",
    (50, 150, 300, 600),
)
STT_RTF = Scale("stt", "speed", "batch_rtf", "batch real-time factor", "", (0.05, 0.1, 0.2, 0.5))
TTS_UTMOS = Scale(
    "tts", "quality", "utmos", "UTMOS naturalness", "", (4.3, 4.0, 3.6, 3.0), higher_is_better=True
)
TTS_FIRST_AUDIO = Scale(
    "tts", "speed", "first_audio.median_ms", "first audio, median", " ms", (100, 200, 400, 800)
)
TTS_RTF = Scale("tts", "speed", "rtf", "real-time factor", "", (0.1, 0.25, 0.5, 1.0))

SCALES: tuple[Scale, ...] = (STT_WER, STT_FINALIZE, STT_RTF, TTS_UTMOS, TTS_FIRST_AUDIO, TTS_RTF)


def rated(kind: Literal["stt", "tts"], metrics: Mapping[str, Any]) -> list[tuple[Scale, float]]:
    """Each scale of ``kind`` with the number it rates in a benchmark
    result's ``metrics``, as the result file holds them; a number that was
    not measured (null) is left out."""
    pairs = []
    for scale in SCALES:
        if scale.kind != kind:
            continue
        value: Any = metrics
        for key in scale.metric.split("."):
            value = value[key]
        if value is not None:
            pairs.append((scale, float(value)))
    return pairs


@dataclass(frozen=True)
class Rating:
    quality: int | None  # None: not measured
    speed: int


def rate(measured: Iterable[tuple[Scale, float]]) -> Rating:
    """The rating of one model from its measured numbers, each with the
    scale it is rated on: quality from the quality scale (None without one),
    speed the lowest speed rating, the slower path a user waits on (an
    engine slower than real time stalls playback whatever its first audio)."""
    quality: int | None = None
    speeds = []
    for scale, value in measured:
        if scale.axis == "quality":
            quality = scale.rate(value)
        else:
            speeds.append(scale.rate(value))
    if not speeds:
        raise ValueError("no speed measurement")
    return Rating(quality, min(speeds))
