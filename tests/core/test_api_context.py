from __future__ import annotations

import asyncio
import time
from typing import NoReturn

import openai
import pytest
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient
from pydantic import BaseModel
from starlette.websockets import WebSocketDisconnect

import voiced.api.app as app_module
from voiced.api.context import WS_CLOSE, APIError, Ctx, require_api_key
from voiced.config import Settings
from voiced.context import ServiceContext
from voiced.engines.base import UnknownVoice
from voiced.inference import inference
from voiced.metrics import Operation, Outcome
from voiced.stt import build_stt_app
from voiced.testing import FakeSTTEngine, lifespan_client
from voiced.threads import run_blocking

HEALTH_KEYS = [
    "status",
    "service",
    "engine",
    "model",
    "model_loaded",
    "uptime_s",
    "queue_depth",
    "queue_capacity",
    "last_inference_at",
    "last_inference_ms",
    "last_error",
    "version",
]


class Body(BaseModel):
    n: int


def _add_test_routes(app: FastAPI) -> None:
    @app.post("/v1/test/body")
    async def body(b: Body) -> dict[str, int]:
        return {"n": b.n}

    @app.post("/v1/test/api-error")
    async def api_error() -> None:
        raise APIError(413, "too big", "payload_too_large")

    @app.post("/v1/test/engine-error")
    async def engine_error(ctx: Ctx) -> None:
        async with inference(ctx, Operation.SPEECH):
            raise UnknownVoice("zz_nobody")

    @app.post("/v1/test/crash")
    async def crash(ctx: Ctx) -> None:
        async with inference(ctx, Operation.SPEECH):
            raise RuntimeError("gpu wedged")

    @app.post("/v1/test/bug")
    async def bug() -> None:
        raise KeyError("outside inference")

    @app.post("/v1/test/hold")
    async def hold(ctx: Ctx) -> None:
        async with inference(ctx, Operation.SPEECH):
            await app.state.gate.wait()


@pytest.fixture
def app(stt_app: FastAPI) -> FastAPI:
    _add_test_routes(stt_app)
    return stt_app


async def test_error_envelope_and_recording(app: FastAPI) -> None:
    ctx = app.state.ctx
    async with lifespan_client(app, raise_app_exceptions=False) as c:
        r = await c.post("/v1/test/body", json={"n": "not a number"})
        assert r.status_code == 400
        assert r.json()["error"]["type"] == "invalid_request_error"
        assert "body.n" in r.json()["error"]["message"]

        r = await c.post("/v1/test/api-error")
        assert (r.status_code, r.json()) == (
            413,
            {"error": {"message": "too big", "type": "payload_too_large"}},
        )

        r = await c.post("/v1/test/engine-error")
        assert r.status_code == 400
        assert r.json()["error"] == {
            "message": "UnknownVoice: zz_nobody",
            "type": "invalid_request_error",
        }
        assert ctx.health.status == "ok"

        app.state.gate = asyncio.Event()
        holders = [asyncio.create_task(c.post("/v1/test/hold")) for _ in range(2)]
        await asyncio.sleep(0.02)
        r = await c.post("/v1/test/hold")  # capacity is 2 in the test settings
        assert r.status_code == 503
        assert r.headers["retry-after"] == "1"
        assert r.json()["error"]["type"] == "server_busy"
        app.state.gate.set()
        assert [h.status_code for h in await asyncio.gather(*holders)] == [200, 200]

        r = await c.post("/v1/test/crash")
        assert (r.status_code, r.json()["error"]["type"]) == (500, "server_error")
        assert ctx.health.status == "degraded"

        r = await c.post("/v1/test/bug")
        assert r.status_code == 500

    ops = ctx.metrics.snapshot()["operations"]
    # Only inference() writes metrics, once per request, by operation.
    assert list(ops) == ["speech"]
    assert ops["speech"]["counts"] == {
        "ok": 2,
        "busy": 1,
        "client_error": 1,
        "disconnect": 0,
        "failure": 1,
    }
    # Health reflects the engine failure only; the bug outside a slot is a 500
    # but not an engine failure.
    assert ctx.health.last_error == "RuntimeError: gpu wedged"


async def test_openai_client_gets_bad_request(app: FastAPI) -> None:
    async with lifespan_client(app) as c:
        client = openai.AsyncOpenAI(api_key="x", base_url="http://voiced/v1", http_client=c)
        with pytest.raises(openai.BadRequestError) as info:
            await client.post("/test/body", body={"n": "x"}, cast_to=object)
    assert info.value.type == "invalid_request_error"


async def test_api_key_401_uses_envelope(app: FastAPI) -> None:
    app.state.ctx.settings.server.api_key = "secret-for-test"
    async with lifespan_client(app) as c:
        r = await c.get("/v1/models")
    assert r.status_code == 401
    assert r.json()["error"]["type"] == "auth_error"


def test_api_key_refuses_websocket_handshake(stt_app: FastAPI) -> None:
    stt_app.state.ctx.settings.server.api_key = "secret-for-test"

    @stt_app.websocket("/v1/test/ws", dependencies=[app_module.Depends(require_api_key)])
    async def ws(websocket: WebSocket) -> None:
        await websocket.accept()
        await websocket.send_text("hi")
        await websocket.close()

    with TestClient(stt_app) as c:
        with pytest.raises(WebSocketDisconnect) as info, c.websocket_connect("/v1/test/ws"):
            pass
        assert info.value.code == 1008
        headers = {"Authorization": "Bearer secret-for-test"}
        with c.websocket_connect("/v1/test/ws", headers=headers) as s:
            assert s.receive_text() == "hi"


def test_engine_error_after_accept_closes_with_ws_close_code(stt_app: FastAPI) -> None:
    @stt_app.websocket("/v1/test/ws-error")
    async def ws(websocket: WebSocket) -> None:
        await websocket.accept()
        raise UnknownVoice("zz_nobody")

    with TestClient(stt_app) as c, c.websocket_connect("/v1/test/ws-error") as s:
        with pytest.raises(WebSocketDisconnect) as info:
            s.receive_text()
    assert info.value.code == WS_CLOSE[Outcome.CLIENT_ERROR] == 1008


async def test_health_answers_starting_while_loading(settings: Settings) -> None:
    app = build_stt_app(settings, FakeSTTEngine(latency_s=0.2))
    async with lifespan_client(app, wait_loaded=False) as c:
        r = await c.get("/health")
        assert (r.status_code, r.json()["status"]) == (503, "starting")
        assert list(r.json()) == HEALTH_KEYS
        r = await c.get("/v1/models")
        assert (r.status_code, r.json()["error"]["type"]) == (503, "server_busy")
        assert r.json()["error"]["message"] == "EngineNotLoaded: model is still loading"
        assert r.headers["retry-after"] == "1"
        await app.state.load_task
        r = await c.get("/health")
        assert (r.status_code, r.json()["status"]) == (200, "ok")
        assert list(r.json()) == HEALTH_KEYS


async def test_blocking_load_in_a_thread_keeps_health_answering(settings: Settings) -> None:
    class SlowLoad(FakeSTTEngine):
        async def load(self) -> None:
            await run_blocking(time.sleep, 0.2)  # the base.py load() rule
            self.loaded = True

    app = build_stt_app(settings, SlowLoad())
    async with lifespan_client(app, wait_loaded=False) as c:
        r = await c.get("/health")
        assert (r.status_code, r.json()["status"]) == (503, "starting")
        await app.state.load_task
        assert (await c.get("/health")).status_code == 200


async def test_health_get_does_not_mutate(stt_app: FastAPI) -> None:
    async with lifespan_client(stt_app) as c:
        before = dict(vars(stt_app.state.ctx.health))
        await c.get("/health")
        assert vars(stt_app.state.ctx.health) == before


async def test_load_failure_exits_non_zero(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Broken(FakeSTTEngine):
        async def load(self) -> None:
            raise RuntimeError("no weights")

    codes: list[int] = []

    def fake_exit(code: int) -> NoReturn:
        codes.append(code)
        raise asyncio.CancelledError  # stop the load task like os._exit would

    monkeypatch.setattr(app_module, "exit_process", fake_exit)
    app = build_stt_app(settings, Broken())
    async with app.router.lifespan_context(app):
        with pytest.raises(asyncio.CancelledError):
            await app.state.load_task
    assert codes == [1]
    assert app.state.ctx.health.model_loaded is False


async def test_shutdown_during_load_exits_without_waiting(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    codes: list[int] = []

    def fake_exit(code: int) -> NoReturn:
        codes.append(code)
        raise SystemExit(code)

    monkeypatch.setattr(app_module, "exit_process", fake_exit)
    app = build_stt_app(settings, FakeSTTEngine(latency_s=10))
    with pytest.raises(SystemExit):
        async with app.router.lifespan_context(app):
            pass
    assert codes == [0]
    app.state.load_task.cancel()


async def test_models_lists_configured_languages(app: FastAPI) -> None:
    ctx: ServiceContext = app.state.ctx
    ctx.settings.stt.languages = ["de"]
    async with lifespan_client(app) as c:
        assert (await c.get("/v1/models")).json()["data"][0]["languages"] == []
