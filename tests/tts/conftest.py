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


@pytest.fixture(scope="session")
def soprano_engine() -> Iterator[object]:
    """A loaded Soprano engine shared by the model tests. Loaded synchronously
    so it can be session scoped regardless of the per-test event loop."""
    from voiced.engines.soprano import SopranoEngine

    engine = SopranoEngine()
    asyncio.run(engine.load())
    yield engine


@pytest.fixture(scope="session")
def phonon_engine() -> Iterator[object]:
    """A loaded Phonon engine shared by the model tests. Loaded synchronously
    so it can be session scoped regardless of the per-test event loop."""
    from voiced.engines.phonon import PhononEngine

    engine = PhononEngine()
    asyncio.run(engine.load())
    yield engine


@pytest.fixture(scope="session")
def pocket_engine() -> Iterator[object]:
    """A loaded Pocket TTS engine shared by the model tests. Loaded
    synchronously so it can be session scoped regardless of the per-test event
    loop."""
    from voiced.engines.pocket import PocketEngine

    engine = PocketEngine()
    asyncio.run(engine.load())
    yield engine


@pytest.fixture(scope="session")
def chatterbox_engine() -> Iterator[object]:
    """A loaded Chatterbox engine shared by the model tests. Loaded synchronously
    so it can be session scoped regardless of the per-test event loop."""
    from voiced.engines.chatterbox import ChatterboxEngine

    engine = ChatterboxEngine()
    asyncio.run(engine.load())
    yield engine


@pytest.fixture(scope="session")
def omnivoice_engine() -> Iterator[object]:
    """A loaded OmniVoice engine shared by the model tests. Loaded synchronously
    so it can be session scoped regardless of the per-test event loop."""
    from voiced.engines.omnivoice import OmniVoiceEngine

    engine = OmniVoiceEngine()
    asyncio.run(engine.load())
    yield engine
