from __future__ import annotations

import os
import socket
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from tests import hf_cache_support as hf
from voiced import models
from voiced.config import CLEARED_HF_ENV, ENGINE_ENV, Settings
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


@pytest.fixture(autouse=True)
def model_cache(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The Hugging Face cache that :func:`voiced.models.hub_dir` names without
    an ``hf_home`` (the settings checks, ``voiced models``, uninstall): an
    empty scratch one, never the real cache, which CI does not have."""
    home = tmp_path_factory.mktemp("hf")
    monkeypatch.setattr("voiced.models.cache.DEFAULT_HF_HOME", home)
    return home / "hub"


@pytest.fixture
def models_downloaded(model_cache: Path) -> Path:
    """Every catalogued repository complete at its pin in :func:`model_cache`."""
    for entry in models.lock().values():
        hf.add_locked(model_cache, entry)
    return model_cache


@pytest.fixture(autouse=True)
def restore_engine_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """``voiced``'s main() sets HF_HOME and the espeak-ng variables from the
    config (apply_engine_environment); in-process calls must not leak them
    into the next test."""
    for key in (*ENGINE_ENV, *CLEARED_HF_ENV):
        monkeypatch.setenv(key, os.environ.get(key, ""))
        if not os.environ[key]:
            monkeypatch.delenv(key)


@pytest.fixture(autouse=True)
def no_port_listeners(monkeypatch: pytest.MonkeyPatch) -> None:
    """``voiced install``'s ports step finds every port free: tests never
    depend on what this machine runs (its own voiced, say). The ports
    step's tests set their own."""
    from voiced.launchd import ports

    monkeypatch.setattr(ports, "bind_error", lambda bind, port: None)
    monkeypatch.setattr(ports, "listener_pids", lambda port, runner: [])


@pytest.fixture(autouse=True)
def no_launchd(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Tests never reach this machine's launchd or its plists: ``launchctl``
    on PATH answers "not found" to everything, ``HOME`` (so
    ``~/Library/LaunchAgents``) is a scratch directory, and so is the
    directory earlier releases' LaunchDaemons are looked for in. Returns
    that directory; tests that need launchd pass a fake runner."""
    from voiced.launchd import control

    root = tmp_path_factory.mktemp("launchd")
    bin_dir = root / "bin"
    bin_dir.mkdir()
    launchctl = bin_dir / "launchctl"
    launchctl.write_text('#!/bin/sh\necho "launchctl is not run in tests" >&2\nexit 113\n')
    launchctl.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")
    home = root / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    daemons = root / "LaunchDaemons"
    daemons.mkdir()
    monkeypatch.setattr(control, "LAUNCH_DAEMONS_DIR", daemons)
    return daemons


def free_port() -> int:
    """An ephemeral TCP port that was free a moment ago."""
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
    # The state dir holds the control socket; never the real ~/.voiced.
    s.install.venv_dir = tmp_path / "state"
    s.stt.engine = "fake"
    s.tts.engine = "fake"
    s.stt.queue_depth = 2
    s.tts.queue_depth = 2
    # Ephemeral Wyoming ports, so a test that runs an app lifespan never binds
    # the real 10300/10200 (which `voiced serve` may hold in another terminal).
    s.stt.wyoming_port = free_port()
    s.tts.wyoming_port = free_port()
    # Same for HTTP: `voiced status` and the watchdog send real health requests
    # to these, and a running voiced on 8880/8881 would answer them.
    s.stt.http_port = free_port()
    s.tts.http_port = free_port()
    return s


@pytest.fixture
def stt_engine() -> FakeSTTEngine:
    return FakeSTTEngine()


@pytest.fixture
def tts_engine() -> FakeTTSEngine:
    return FakeTTSEngine()


@pytest.fixture
def stt_app(settings: Settings, stt_engine: FakeSTTEngine) -> FastAPI:
    return build_stt_app(settings, stt_engine)


@pytest.fixture
def tts_app(settings: Settings, tts_engine: FakeTTSEngine) -> FastAPI:
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
