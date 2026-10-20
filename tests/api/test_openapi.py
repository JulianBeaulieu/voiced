"""``GET /openapi.json`` and ``GET /docs`` on both services."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from voiced.testing import lifespan_client

EXPECTED_PATHS = {
    "stt": {"/health", "/metrics.json", "/v1/models", "/v1/audio/transcriptions"},
    "tts": {"/health", "/metrics.json", "/v1/models", "/v1/audio/speech", "/v1/audio/voices"},
}


@pytest.mark.parametrize("service", ["stt", "tts"])
async def test_openapi_json_describes_the_api_without_a_key(
    service: str, stt_app: FastAPI, tts_app: FastAPI
) -> None:
    app = stt_app if service == "stt" else tts_app
    app.state.ctx.settings.server.api_key = "s3cret"
    async with lifespan_client(app) as client:
        r = await client.get("/openapi.json")
    assert r.status_code == 200, r.text
    assert EXPECTED_PATHS[service] <= set(r.json()["paths"])


async def test_speech_documents_its_audio_stream(tts_client: AsyncClient) -> None:
    doc = (await tts_client.get("/openapi.json")).json()
    ok = doc["paths"]["/v1/audio/speech"]["post"]["responses"]["200"]
    assert "audio/wav" in ok["content"]


async def test_docs_page_loads(any_client: AsyncClient) -> None:
    r = await any_client.get("/docs")
    assert r.status_code == 200
