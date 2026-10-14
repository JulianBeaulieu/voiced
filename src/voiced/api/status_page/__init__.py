"""The status page (``GET /``) and its data feed (``GET /metrics.json``).

The page is one self-contained HTML file with vanilla JavaScript and a few
Lucide icons inlined as SVG. There is no build step and nothing is fetched
from a CDN at runtime. It polls ``/metrics.json`` which merges the
``/health`` snapshot with the per-operation latency and error metrics
(``voiced.metrics``, which also reports resident memory) and engine
metadata (license, languages). Both routes are
unauthenticated, like ``/health``; the test boxes on the page call the
``/v1`` endpoints and send the API key the operator types in, if one is set.

With ``[server] browser_settings = true`` the page also shows a settings
form backed by ``GET /settings.json`` and ``POST /settings``. It changes the
config file through the same function as the control socket
(``voiced.control.ops.change_settings``) and is gated by
:func:`form_refusal`; see ADR 0013.
"""

from __future__ import annotations

import asyncio
import hmac
from importlib import resources
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Request, status
from fastapi.responses import HTMLResponse

from voiced.api.context import APIError, Ctx
from voiced.config import SETTABLE, Settings, bind_mode, config_path, load_settings
from voiced.context import ServiceContext
from voiced.control.ops import change_result, change_settings, engines_list, settings_view
from voiced.control.protocol import INVALID_SETTING, OpError

router = APIRouter(tags=["service"])

PAGE_HTML = resources.files(__package__).joinpath("page.html").read_text(encoding="utf-8")


@router.get("/", include_in_schema=False, response_class=HTMLResponse)
async def status_page() -> HTMLResponse:
    return HTMLResponse(PAGE_HTML, headers={"Cache-Control": "no-store"})


@router.get("/metrics.json", summary="Status page data")
async def metrics_json(ctx: Ctx) -> dict[str, Any]:
    """Everything the status page shows, in one document.

    ``health`` is the exact ``/health`` snapshot. ``metrics`` is the
    ``voiced.metrics`` snapshot: per-operation outcome counts and p50/p95
    latency, the recent error log, and resident memory.
    """
    engine = ctx.engine
    return {
        "service": ctx.service,
        "engine": {
            "name": engine.name,
            "model": engine.model_id,
            "license": engine.license,
            "languages": ctx.languages,
        },
        "health": ctx.health.snapshot(ctx.queue.depth).model_dump(),
        "metrics": _public_metrics(ctx),
        "api_key_required": bool(ctx.settings.server.api_key),
        "default_voice": ctx.settings.tts.default_voice if ctx.service == "tts" else None,
        "settings_form": await asyncio.to_thread(_form_state, ctx.settings),
    }


def _public_metrics(ctx: ServiceContext) -> dict[str, Any]:
    """The metrics snapshot, minus request content when an API key is set.

    An error's ``detail`` can echo what a client sent (an unknown language
    tag, a voice name). This feed is unauthenticated, so with
    ``server.api_key`` set it keeps each error's time, operation and outcome
    but not the detail, which would otherwise leak requests past the key.
    """
    snapshot = ctx.metrics.snapshot()
    if ctx.settings.server.api_key:
        snapshot["recent_errors"] = [
            {k: v for k, v in error.items() if k != "detail"} for error in snapshot["recent_errors"]
        ]
    return snapshot


# Settings form ---------------------------------------------------------------

FORM_OFF = (
    "The settings form is off. Turn it on with `voiced settings set browser_settings=true` "
    "on the server."
)
FORM_LAN_WITHOUT_KEY = (
    "The settings form is refused: this server listens on the network (bind is lan) and no "
    "API key is set, so anyone on the network could change it. Set server.api_key, or change "
    "settings with `voiced settings` or the menu bar app on the server itself."
)


def form_refusal(running: Settings, live: Settings, authorization: str) -> APIError | None:
    """Why a settings form request must be refused, or None to allow it.

    ``running`` is what this process was started with, ``live`` the config
    file now; a pending restart must not open a gap, so the stricter of the
    two applies: either one on the LAN counts as LAN, and either one's API
    key is accepted while both are pending. The on/off switch is read live,
    so turning the form off takes effect at once.
    """
    if not live.server.browser_settings:
        return APIError(status.HTTP_404_NOT_FOUND, FORM_OFF, "settings_disabled")
    keys = [k for k in (live.server.api_key, running.server.api_key) if k]
    on_lan = "lan" in (bind_mode(live.server.bind), bind_mode(running.server.bind))
    if on_lan and not keys:
        return APIError(status.HTTP_403_FORBIDDEN, FORM_LAN_WITHOUT_KEY, "settings_refused")
    if keys:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not any(
            hmac.compare_digest(token.encode(), k.encode()) for k in keys
        ):
            return APIError(
                status.HTTP_401_UNAUTHORIZED, "invalid or missing API key", "auth_error"
            )
    return None


def _live() -> Settings | None:
    try:
        return load_settings(config_path())
    except Exception:
        return None


def _form_state(running: Settings) -> dict[str, Any]:
    """What the page needs to decide whether to draw the form."""
    live = _live()
    if live is None or not live.server.browser_settings:
        return {"enabled": False, "refused": None, "api_key_required": False}
    refusal = form_refusal(running, live, "")
    refused = refusal.message if refusal and refusal.type == "settings_refused" else None
    keyed = bool(live.server.api_key or running.server.api_key)
    return {"enabled": True, "refused": refused, "api_key_required": keyed}


def _guard(request: Request, running: Settings) -> Settings:
    origin = request.headers.get("origin")
    if origin and urlsplit(origin).netloc != request.headers.get("host", ""):
        raise APIError(status.HTTP_403_FORBIDDEN, "cross-origin request refused", "auth_error")
    live = _live()
    if live is None:
        raise APIError(
            status.HTTP_500_INTERNAL_SERVER_ERROR, "config file unreadable", "server_error"
        )
    refusal = form_refusal(running, live, request.headers.get("authorization", ""))
    if refusal is not None:
        raise refusal
    return live


@router.get("/settings.json", summary="Settings form data", include_in_schema=False)
async def settings_json(request: Request, ctx: Ctx) -> dict[str, Any]:
    live = await asyncio.to_thread(_guard, request, ctx.settings)
    return {
        "settings": settings_view(live, config_path()),
        "engines": await asyncio.to_thread(engines_list),
        "settable": list(SETTABLE),
    }


@router.post("/settings", summary="Change settings", include_in_schema=False)
async def settings_post(request: Request, ctx: Ctx) -> dict[str, Any]:
    """Same rules and result as ``settings.set`` on the control socket."""
    await asyncio.to_thread(_guard, request, ctx.settings)
    if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
        raise APIError(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "send application/json")
    try:
        body = await request.json()
    except ValueError:
        raise APIError(status.HTTP_400_BAD_REQUEST, "body is not JSON") from None
    if not isinstance(body, dict) or not body:
        raise APIError(status.HTTP_400_BAD_REQUEST, "send an object of settings to change")
    path = config_path()
    try:
        change = await asyncio.to_thread(change_settings, body, path)
    except OpError as exc:
        if exc.code == INVALID_SETTING:
            field = exc.extra.get("field", "")
            raise APIError(
                status.HTTP_400_BAD_REQUEST, f"{field}: {exc.message}", exc.code
            ) from None
        raise APIError(status.HTTP_500_INTERNAL_SERVER_ERROR, exc.message, "server_error") from None
    return change_result(change, path)
