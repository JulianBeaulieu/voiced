"""``GET /`` (status page) and ``GET /metrics.json``."""

from __future__ import annotations

import re

from httpx import AsyncClient

from voiced import __version__

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


async def test_status_page_is_not_behind_api_key(stt_app) -> None:  # type: ignore[no-untyped-def]
    from voiced.testing import lifespan_client

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


async def test_metrics_json_tracks_requests_and_errors(tts_client: AsyncClient) -> None:
    assert (await tts_client.post("/v1/audio/speech", json={"input": "Hi."})).status_code == 200
    assert (
        await tts_client.post("/v1/audio/speech", json={"input": "Hi.", "voice": "nobody"})
    ).status_code == 400
    doc = (await tts_client.get("/metrics.json")).json()
    speech = doc["metrics"]["operations"]["speech"]
    assert speech["counts"]["ok"] == 1 and speech["counts"]["client_error"] == 1
    assert speech["latency"]["samples"] == 1
    assert speech["latency"]["p50_ms"] == speech["latency"]["p95_ms"]
    assert speech["latency"]["p50_ms"] >= 0
    (err,) = doc["metrics"]["recent_errors"]
    assert err["operation"] == "speech" and err["outcome"] == "client_error"
    assert "nobody" in err["detail"]
    assert doc["health"]["last_inference_ms"] is not None
