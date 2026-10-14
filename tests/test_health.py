from __future__ import annotations

import pytest
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


@pytest.mark.parametrize(
    ("service", "method", "path"),
    [
        ("stt", "POST", "/v1/audio/transcriptions"),
        ("tts", "POST", "/v1/audio/speech"),
        ("tts", "GET", "/v1/audio/voices"),
        ("tts", "GET", "/"),
        ("stt", "GET", "/"),
    ],
)
async def test_unimplemented_routes_return_501(
    service: str, method: str, path: str, stt_app: FastAPI, tts_app: FastAPI
) -> None:
    app = stt_app if service == "stt" else tts_app
    async with lifespan_client(app) as c:
        r = await c.request(method, path)
    assert r.status_code == 501
    assert r.json()["error"]["type"] == "not_implemented"


async def test_api_key_guards_v1_but_not_health(stt_app: FastAPI) -> None:
    stt_app.state.ctx.settings.server.api_key = "secret-for-test"
    async with lifespan_client(stt_app) as c:
        assert (await c.get("/health")).status_code == 200
        assert (await c.get("/v1/models")).status_code == 401
        ok = await c.get("/v1/models", headers={"Authorization": "Bearer secret-for-test"})
        assert ok.status_code == 200
