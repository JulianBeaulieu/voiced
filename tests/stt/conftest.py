"""Shared helpers for the STT tests. The ``model`` marker is handled in ``tests/conftest.py``."""

from __future__ import annotations

import re
import wave
from difflib import SequenceMatcher
from pathlib import Path

import numpy as np
import pytest
from numpy.typing import NDArray

from voiced.engines.base import STT_SAMPLE_RATE

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def load_fixture(name: str) -> NDArray[np.float32]:
    """Read a 16 kHz mono 16-bit WAV from ``tests/fixtures`` as float32."""
    with wave.open(str(FIXTURES / name)) as wav:
        assert wav.getframerate() == STT_SAMPLE_RATE
        assert wav.getnchannels() == 1
        assert wav.getsampwidth() == 2
        pcm = wav.readframes(wav.getnframes())
    return np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0


def words(text: str) -> list[str]:
    return re.sub(r"[^a-z0-9 ]", " ", text.lower()).split()


def similarity(a: str, b: str) -> float:
    """Word-level similarity in [0, 1], case and punctuation insensitive."""
    return SequenceMatcher(None, words(a), words(b)).ratio()


def word_accuracy(hypothesis: str, reference: str) -> float:
    """1 minus the word error rate (edit distance over the reference length).
    Unlike :func:`similarity` it aligns optimally, so text that repeats itself
    is scored right."""
    hyp, ref = words(hypothesis), words(reference)
    row = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        prev, row[0] = row[0], i
        for j, h in enumerate(hyp, 1):
            prev, row[j] = row[j], min(row[j] + 1, row[j - 1] + 1, prev + (r != h))
    return 1.0 - row[-1] / max(len(ref), 1)


@pytest.fixture
def speech() -> NDArray[np.float32]:
    return load_fixture("speech_en.wav")


@pytest.fixture
def speech_with_trailing_silence() -> NDArray[np.float32]:
    return load_fixture("speech_en_trailing_silence.wav")
