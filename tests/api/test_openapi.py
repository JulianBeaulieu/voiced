"""``GET /openapi.json`` and ``GET /docs`` on both services."""

from __future__ import annotations

import pytest
from fastapi import FastAPI

from voiced.testing import lifespan_client

EXPECTED_PATHS = {
    "stt": {"/health", "/metrics.json", "/v1/models", "/v1/audio/transcriptions"},
    "tts": {"/health", "/metrics.json", "/v1/models", "/v1/audio/speech", "/v1/audio/voices"},
}


@pytest.mark.parametrize("service", ["stt", "tts"])
@pytest.mark.parametrize("api_key", [None, "s3cret"])
async def test_openapi_json_describes_the_api(
    service: str, api_key: str | None, stt_app: FastAPI, tts_app: FastAPI
) -> None:
    app = stt_app if service == "stt" else tts_app
    app.state.ctx.settings.server.api_key = api_key
    async with lifespan_client(app) as client:
        r = await client.get("/openapi.json")
    assert r.status_code == 200, r.text
    doc = r.json()
    assert doc["openapi"].startswith("3.")
    assert EXPECTED_PATHS[service] <= set(doc["paths"])


async def test_speech_documents_its_audio_stream(tts_app: FastAPI) -> None:
    async with lifespan_client(tts_app) as client:
        doc = (await client.get("/openapi.json")).json()
    ok = doc["paths"]["/v1/audio/speech"]["post"]["responses"]["200"]
    assert "audio/wav" in ok["content"]


@pytest.mark.parametrize("service", ["stt", "tts"])
async def test_docs_page_points_at_the_schema(
    service: str, stt_app: FastAPI, tts_app: FastAPI
) -> None:
    app = stt_app if service == "stt" else tts_app
    async with lifespan_client(app) as client:
        r = await client.get("/docs")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "/openapi.json" in r.text
