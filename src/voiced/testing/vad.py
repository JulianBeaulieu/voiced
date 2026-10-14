"""A deterministic speech detector for endpointing tests.

Silero rejects white noise and tones as non-speech, which is what you want in
production and useless for synthetic test signals. :class:`EnergyDetector`
calls anything above an RMS threshold speech, so tests can build "speech" from
noise bursts and assert on exact frame positions.
"""

from __future__ import annotations

import numpy as np
from numpy.typing import NDArray

from voiced.stt.vad import FRAME_SAMPLES


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
