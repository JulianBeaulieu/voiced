"""Fixtures for the TTS engine tests. The ``model`` auto-skip lives in
``tests/conftest.py``."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest


@pytest.fixture(scope="session")
def kokoro_engine() -> Iterator[object]:
    """A loaded Kokoro engine shared by the model tests. Loaded synchronously
    so it can be session scoped regardless of the per-test event loop."""
    from voiced.engines.kokoro import KokoroEngine

    engine = KokoroEngine()
    asyncio.run(engine.load())
    yield engine
