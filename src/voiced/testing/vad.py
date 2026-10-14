"""A deterministic speech detector for endpointing tests.

Silero rejects white noise and tones as non-speech, which is what you want in
production and useless for synthetic test signals. :class:`EnergyDetector`
calls anything above an RMS threshold speech, so tests can build "speech" from
noise bursts and assert on exact frame positions.
"""

from __future__ import annotations

import numpy as np
from numpy.typing import NDArray

from voiced.audio import VAD_FRAME_SAMPLES, dbfs_to_amplitude, frame_rms


class EnergyDetector:
    """RMS threshold. ``threshold_dbfs`` defaults to -40 dBFS."""

    frame_samples = VAD_FRAME_SAMPLES

    def __init__(self, threshold_dbfs: float = -40.0) -> None:
        if threshold_dbfs >= 0:
            raise ValueError("threshold_dbfs must be below 0 dBFS")
        self.threshold = dbfs_to_amplitude(threshold_dbfs)

    def probability(self, frame: NDArray[np.float32]) -> float:
        return 1.0 if float(np.max(frame_rms(frame), initial=0.0)) >= self.threshold else 0.0

    def reset(self) -> None:
        return None
