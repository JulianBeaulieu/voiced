"""Fixtures for the TTS tests.

Tests marked ``model`` need the real weights, espeak-ng and Metal. They are
skipped unless ``VOICED_MODEL_TESTS=1`` so the default ``pytest`` run passes
on CI, which has neither the ``tts`` extra nor a GPU.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator

import pytest

MODEL_TESTS_ENV = "VOICED_MODEL_TESTS"


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if os.environ.get(MODEL_TESTS_ENV) == "1":
        return
    skip = pytest.mark.skip(reason=f"needs real models; set {MODEL_TESTS_ENV}=1")
    for item in items:
        if "model" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(scope="session")
def kokoro_engine() -> Iterator[object]:
    """A loaded Kokoro engine shared by the model tests. Loaded synchronously
    so it can be session scoped regardless of the per-test event loop."""
    from voiced.engines.kokoro import KokoroEngine

    engine = KokoroEngine()
    asyncio.run(engine.load())
    yield engine
