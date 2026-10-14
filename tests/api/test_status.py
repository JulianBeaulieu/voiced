"""``GET /`` (status page) and ``GET /metrics.json``."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator

from fastapi import FastAPI
from httpx import AsyncClient

from voiced import __version__
from voiced.config import Settings
from voiced.engines.base import EngineError, PCMChunk
from voiced.testing import FakeTTSEngine, lifespan_client
from voiced.tts import build_tts_app

_EMOJI = re.compile(f"[{chr(0x1F000)}-{chr(0x1FAFF)}{chr(0x2600)}-{chr(0x27BF)}]")


async def test_status_page_is_self_contained_html(any_client: AsyncClient) -> None:
    r = await any_client.get("/")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    html = r.text
    assert "<!doctype html>" in html.lower()
    assert "voiced" in html
    assert "/metrics.json" in html
    assert '<symbol id="i-mic"' in html and '<symbol id="i-volume"' in html
    assert "https://" not in html.replace("https://lucide.dev", ""), "no remote asset at runtime"
    assert "<script src" not in html and "<link" not in html
    assert not _EMOJI.search(html)


async def test_status_page_is_not_behind_api_key(stt_app: FastAPI) -> None:
    stt_app.state.ctx.settings.server.api_key = "s3cret"
    async with lifespan_client(stt_app) as c:
        assert (await c.get("/")).status_code == 200
        doc = (await c.get("/metrics.json")).json()
        assert doc["api_key_required"] is True


async def test_metrics_json_shape(tts_client: AsyncClient) -> None:
    doc = (await tts_client.get("/metrics.json")).json()
    assert doc["service"] == "tts"
    assert doc["engine"] == {
        "name": "fake",
        "model": "fake/tts",
        "license": "MIT",
        "languages": ["en"],
    }
    assert doc["health"]["status"] == "ok"
    assert doc["health"]["version"] == __version__
    assert doc["health"]["queue_capacity"] == 2
    assert doc["metrics"]["memory"]["rss_bytes"] > 0
    assert doc["metrics"]["operations"] == {}
    assert doc["metrics"]["recent_errors"] == []
    assert doc["default_voice"] == "af_heart"
    assert doc["api_key_required"] is False


class _Refuses(FakeTTSEngine):
    """Raises a client error from inside synthesis, so it happens within the slot."""

    async def synthesize(
        self, text: str, voice: str, speed: float = 1.0
    ) -> AsyncIterator[PCMChunk]:
        if "refuse" in text:
            raise EngineError("this engine refuses that text")
        async for chunk in super().synthesize(text, voice, speed):
            yield chunk


async def test_metrics_json_tracks_requests_and_errors(settings: Settings) -> None:
    app = build_tts_app(settings, _Refuses())
    async with lifespan_client(app) as c:
        assert (await c.post("/v1/audio/speech", json={"input": "Hi."})).status_code == 200
        refused = await c.post("/v1/audio/speech", json={"input": "Please refuse this."})
        assert refused.status_code == 400
        doc = (await c.get("/metrics.json")).json()
    speech = doc["metrics"]["operations"]["speech"]
    assert speech["counts"]["ok"] == 1 and speech["counts"]["client_error"] == 1
    assert speech["latency"]["samples"] == 1
    assert speech["latency"]["p50_ms"] == speech["latency"]["p95_ms"]
    assert speech["latency"]["p50_ms"] >= 0
    (err,) = doc["metrics"]["recent_errors"]
    assert err["operation"] == "speech" and err["outcome"] == "client_error"
    assert "refuses that text" in err["detail"]
    assert doc["health"]["last_inference_ms"] is not None
    assert doc["health"]["status"] == "ok", "client errors leave health alone"


async def test_error_detail_is_hidden_from_the_open_feed_when_a_key_is_set(
    settings: Settings,
) -> None:
    """Error detail can echo request content; with a key set the open feed omits it."""
    settings.server.api_key = "s3cret"
    app = build_tts_app(settings, _Refuses())
    auth = {"Authorization": "Bearer s3cret"}
    async with lifespan_client(app) as c:
        r = await c.post("/v1/audio/speech", json={"input": "refuse jane-doe-123"}, headers=auth)
        assert r.status_code == 400
        doc = (await c.get("/metrics.json")).json()  # no key: the status page's view
    (err,) = doc["metrics"]["recent_errors"]
    assert set(err) == {"at", "operation", "outcome"}
    assert "jane-doe" not in str(doc)
    assert doc["metrics"]["operations"]["speech"]["counts"]["client_error"] == 1
