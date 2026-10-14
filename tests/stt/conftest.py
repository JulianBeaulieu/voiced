"""Shared helpers for the STT tests.

Tests marked ``model`` need the real Parakeet weights and Metal. They are
skipped unless ``VOICED_MODEL_TESTS=1`` so the default ``pytest`` run passes
on a CI runner without the ``stt`` extra.
"""

from __future__ import annotations

import os
import re
import wave
from difflib import SequenceMatcher
from pathlib import Path

import numpy as np
import pytest
from numpy.typing import NDArray

from voiced.engines.base import STT_SAMPLE_RATE

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
MODEL_TESTS_ENV = "VOICED_MODEL_TESTS"


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if os.environ.get(MODEL_TESTS_ENV) == "1":
        return
    skip = pytest.mark.skip(reason=f"needs real model weights; set {MODEL_TESTS_ENV}=1")
    for item in items:
        if "model" in item.keywords:
            item.add_marker(skip)


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


@pytest.fixture
def speech() -> NDArray[np.float32]:
    return load_fixture("speech_en.wav")


@pytest.fixture
def speech_with_trailing_silence() -> NDArray[np.float32]:
    return load_fixture("speech_en_trailing_silence.wav")
