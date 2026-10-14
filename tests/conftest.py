from __future__ import annotations

import os
import socket
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from voiced.config import Settings
from voiced.stt import build_stt_app
from voiced.testing import FakeSTTEngine, FakeTTSEngine, lifespan_client
from voiced.tts import build_tts_app

MODEL_TESTS_ENV = "VOICED_MODEL_TESTS"


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Tests marked ``model`` need real weights and Metal; skip them unless
    ``VOICED_MODEL_TESTS=1`` so the default run passes on CI."""
    if os.environ.get(MODEL_TESTS_ENV) == "1":
        return
    skip = pytest.mark.skip(reason=f"needs real models; set {MODEL_TESTS_ENV}=1")
    for item in items:
        if item.get_closest_marker("model") is not None:
            item.add_marker(skip)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
    return port


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    """Settings with the fake engines, a temp log dir, and no env leakage."""
    for key in list(os.environ):
        if key.startswith("VOICED_"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("VOICED_CONFIG", str(tmp_path / "missing.toml"))
    s = Settings()
    s.server.log_dir = tmp_path / "logs"
    s.stt.engine = "fake"
    s.tts.engine = "fake"
    s.stt.queue_depth = 2
    s.tts.queue_depth = 2
    return s


@pytest.fixture
def stt_engine() -> FakeSTTEngine:
    return FakeSTTEngine()


@pytest.fixture
def tts_engine() -> FakeTTSEngine:
    return FakeTTSEngine()


@pytest.fixture
def stt_app(settings: Settings, stt_engine: FakeSTTEngine) -> FastAPI:
    # Ephemeral Wyoming port so running the app lifespan in a test never binds
    # the real 10300 (which may be in use by `voiced serve` elsewhere).
    settings.stt.wyoming_port = _free_port()
    return build_stt_app(settings, stt_engine)


@pytest.fixture
def tts_app(settings: Settings, tts_engine: FakeTTSEngine) -> FastAPI:
    settings.tts.wyoming_port = _free_port()
    return build_tts_app(settings, tts_engine)


@pytest.fixture
async def stt_client(stt_app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with lifespan_client(stt_app) as c:
        yield c


@pytest.fixture
async def tts_client(tts_app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with lifespan_client(tts_app) as c:
        yield c


@pytest.fixture(params=["stt", "tts"])
async def any_client(
    request: pytest.FixtureRequest, stt_app: FastAPI, tts_app: FastAPI
) -> AsyncIterator[AsyncClient]:
    """Parametrized over both services."""
    app = stt_app if request.param == "stt" else tts_app
    async with lifespan_client(app) as c:
        yield c
