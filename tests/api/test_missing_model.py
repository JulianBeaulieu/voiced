"""A service whose model is not downloaded stays up as ``missing_model``
(ADR 0022): ``/health`` says so, work is refused with
``model_not_downloaded`` and no ``Retry-After``, and the process does not exit."""

from __future__ import annotations

import pytest
from fastapi import FastAPI

from voiced.config import Settings
from voiced.inference import classify
from voiced.metrics import Outcome
from voiced.models import ModelNotDownloaded
from voiced.stt import build_stt_app
from voiced.testing import FakeSTTEngine, lifespan_client

REPO = "mlx-community/parakeet-tdt-0.6b-v3"


class Missing(FakeSTTEngine):
    def __init__(self, error: Exception) -> None:
        super().__init__()
        self.model_id = REPO
        self.error = error

    async def load(self) -> None:
        raise self.error


def _app(settings: Settings, error: Exception) -> FastAPI:
    return build_stt_app(settings, Missing(error))


async def test_a_missing_model_is_reported_and_the_service_stays_up(settings: Settings) -> None:
    app = _app(settings, ModelNotDownloaded(REPO, "e" * 40))
    async with lifespan_client(app) as c:  # no exit_process: the fixture would raise
        r = await c.get("/health")
        assert r.status_code == 503
        body = r.json()
        assert body["status"] == "missing_model" and body["model_loaded"] is False
        assert REPO in body["last_error"] and f"voiced models download {REPO}" in body["last_error"]
        r = await c.get("/v1/models")
        assert r.status_code == 503 and "retry-after" not in r.headers
        assert r.json()["error"]["type"] == "model_not_downloaded"
        assert r.json()["error"]["message"].startswith(f"ModelNotDownloaded: {REPO} is not")


async def test_the_hubs_offline_error_inside_the_engine_is_a_missing_model(
    settings: Settings,
) -> None:
    huggingface_hub = pytest.importorskip("huggingface_hub")
    try:
        raise huggingface_hub.errors.LocalEntryNotFoundError("offline")
    except Exception as inner:
        wrapped = RuntimeError("could not load")
        wrapped.__cause__ = inner
    app = _app(settings, wrapped)
    async with lifespan_client(app) as c:
        body = (await c.get("/health")).json()
    assert body["status"] == "missing_model"
    assert REPO in body["last_error"]  # the configured model the engine was loading


async def test_with_an_api_key_the_status_still_names_the_model(settings: Settings) -> None:
    """``last_error`` loses its message; ``model`` is where the app reads the repository."""
    settings.server.api_key = "k"
    app = _app(settings, ModelNotDownloaded(REPO))
    async with lifespan_client(app) as c:
        body = (await c.get("/health")).json()
    assert body["status"] == "missing_model" and body["model"] == REPO
    assert REPO not in body["last_error"]


def test_a_missing_model_is_not_an_engine_failure() -> None:
    """Inside a slot (a Wyoming stream checks there) it is counted busy and
    never marks the service degraded."""
    assert classify(ModelNotDownloaded(REPO)) is Outcome.BUSY


async def test_a_missing_shared_model_names_the_command_that_fetches_it(
    settings: Settings,
) -> None:
    """Phonon's voices alone missing: the command is the model's, which
    ``voiced models download`` takes, and the voices are named."""
    engine = Missing(ModelNotDownloaded("gradium/phonon-voices", "d" * 40))
    engine.model_id = "gradium/phonon-31m-en"
    app = build_stt_app(settings, engine)
    async with lifespan_client(app) as c:
        body = (await c.get("/health")).json()
        error = (await c.get("/v1/models")).json()["error"]["message"]
    assert body["status"] == "missing_model"
    for text in (body["last_error"], error):
        assert (
            "(gradium/phonon-voices missing); run voiced models download gradium/phonon-31m-en"
            in text
        )


async def test_offline_mode_in_a_plugin_is_a_missing_model(settings: Settings) -> None:
    """A plugin calling the Hub under HF_HUB_OFFLINE must not restart every 10 s."""
    huggingface_hub = pytest.importorskip("huggingface_hub")
    app = _app(settings, huggingface_hub.errors.OfflineModeIsEnabled("offline"))
    async with lifespan_client(app) as c:
        assert (await c.get("/health")).json()["status"] == "missing_model"
