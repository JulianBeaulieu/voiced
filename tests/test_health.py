from __future__ import annotations

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from voiced import __version__
from voiced.health import HealthResponse
from voiced.testing import lifespan_client


async def test_health_schema_when_loaded(any_client: AsyncClient) -> None:
    r = await any_client.get("/health")
    assert r.status_code == 200
    body = HealthResponse.model_validate(r.json())
    assert body.status == "ok"
    assert body.model_loaded is True
    assert body.engine == "fake"
    assert body.queue_depth == 0
    assert body.queue_capacity == 2
    assert body.version == __version__
    assert body.last_inference_at is None


async def test_health_503_before_load(stt_app: FastAPI) -> None:
    # No lifespan: the engine is never loaded.
    async with AsyncClient(transport=ASGITransport(app=stt_app), base_url="http://t") as c:
        r = await c.get("/health")
    assert r.status_code == 503
    assert r.json()["status"] == "starting"


async def test_models_lists_engine_metadata(tts_client: AsyncClient) -> None:
    r = await tts_client.get("/v1/models")
    assert r.status_code == 200
    (model,) = r.json()["data"]
    assert model["id"] == "fake/tts"
    assert model["languages"] == ["en"]
    assert model["license"] == "MIT"
    assert model["task"] == "speech"


async def test_api_key_guards_v1_but_not_health(stt_app: FastAPI) -> None:
    stt_app.state.ctx.settings.server.api_key = "secret-for-test"
    async with lifespan_client(stt_app) as c:
        assert (await c.get("/health")).status_code == 200
        assert (await c.get("/v1/models")).status_code == 401
        ok = await c.get("/v1/models", headers={"Authorization": "Bearer secret-for-test"})
        assert ok.status_code == 200


async def test_health_idle_is_200_and_not_loaded(stt_client: AsyncClient, stt_app: FastAPI) -> None:
    stt_app.state.ctx.health.mark_unloaded()
    r = await stt_client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "idle"
    assert r.json()["model_loaded"] is False


def test_status_table() -> None:
    """ADR 0012's table, extended by ADR 0033: what each state reports."""
    from voiced.health import READY_STATUSES, HealthState
    from voiced.models import ModelNotDownloaded

    h = HealthState(service="stt", engine="e", model="m", version="v", queue_capacity=1)
    assert h.status == "starting" and "starting" not in READY_STATUSES
    h.mark_loaded()
    assert h.status == "ok"
    h.mark_unloaded()
    assert h.status == "idle" and not h.model_loaded and h.started
    h.reloading = True
    assert h.status == "loading"
    h.reloading = False
    h.record_error(RuntimeError("load failed"))
    assert h.status == "degraded", "an error outranks idle"
    h.mark_loaded()
    assert h.status == "ok" and not h.unloaded
    h.mark_missing(ModelNotDownloaded("m", "r", ("m",)))
    assert h.status == "missing_model"
    assert READY_STATUSES == {"ok", "degraded", "idle", "loading"}
