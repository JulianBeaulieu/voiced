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
(``voiced.settings_store.apply_settings``) and is gated by
:func:`form_refusal`: it always needs the API key, so with no key set it is
refused, on loopback too (ADR 0013). The watchdog notices the changed file
and tells its control socket clients.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import threading
from importlib import resources
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request, status
from fastapi.responses import HTMLResponse

from voiced.api.context import APIError, Ctx
from voiced.config import Settings, config_path, load_settings
from voiced.context import ServiceContext
from voiced.engines.catalogue import engines_list
from voiced.settings_store import (
    SETTABLE,
    ConfigFileError,
    FileStamp,
    InvalidSetting,
    apply_settings,
    change_view,
    file_stamp,
    settings_view,
)

log = logging.getLogger(__name__)

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
FORM_NEEDS_KEY = (
    "The settings form needs an API key, and none is set. Without one, any web page you "
    "open could change this server through your browser. Set a key on the server with "
    "`voiced settings set api_key=-`, or change settings there with `voiced settings` or "
    "the menu bar app."
)


def form_refusal(running: Settings, live: Settings, authorization: str) -> APIError | None:
    """Why a settings form request must be refused, or None to allow it.

    The form always needs the API key as a bearer token: a page on the LAN
    or a DNS-rebinding page in the operator's own browser can reach the
    port, and only the key tells them apart. ``running`` is what this
    process was started with, ``live`` the config file now; while a key
    change waits for a restart, either key is accepted. The on/off switch is
    read live, so turning the form off takes effect at once.
    """
    if not live.server.browser_settings:
        return APIError(status.HTTP_404_NOT_FOUND, FORM_OFF, "settings_disabled")
    keys = [k for k in (live.server.api_key, running.server.api_key) if k]
    if not keys:
        return APIError(status.HTTP_403_FORBIDDEN, FORM_NEEDS_KEY, "settings_refused")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not any(
        hmac.compare_digest(token.encode(), k.encode()) for k in keys
    ):
        return APIError(status.HTTP_401_UNAUTHORIZED, "invalid or missing API key", "auth_error")
    return None


class _LiveSettings:
    """The config file's settings, re-read only when the file changes, so
    ``/metrics.json`` polls do not parse it every time."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stamp: tuple[Path, FileStamp | None] | None = None
        self._settings: Settings | None = None

    def get(self) -> Settings | None:
        path = config_path()
        stamp = (path, file_stamp(path))
        with self._lock:
            if self._settings is not None and stamp == self._stamp:
                return self._settings
            try:
                settings = load_settings(path)
            except (OSError, ValueError) as exc:  # TOMLDecodeError, ValidationError
                log.warning("settings form: cannot read %s: %s", path, exc)
                return None
            self._stamp, self._settings = stamp, settings
            return settings


_live = _LiveSettings()


def _form_state(running: Settings) -> dict[str, Any]:
    """What the page needs to decide whether to draw the form. The form
    always needs the API key, so the key box shows whenever it is drawn."""
    live = _live.get()
    if live is None or not live.server.browser_settings:
        return {"enabled": False, "refused": None}
    refusal = form_refusal(running, live, "")
    refused = refusal.message if refusal and refusal.type == "settings_refused" else None
    return {"enabled": True, "refused": refused}


def _guard(request: Request, running: Settings) -> Settings:
    live = _live.get()
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


def _apply(changes: dict[str, object], path: Path) -> dict[str, Any]:
    try:
        change = apply_settings(changes, path, engines=engines_list)
    except InvalidSetting as exc:
        raise APIError(
            status.HTTP_400_BAD_REQUEST, f"{exc.field}: {exc.message}", "invalid_setting"
        ) from None
    except ConfigFileError as exc:
        raise APIError(status.HTTP_500_INTERNAL_SERVER_ERROR, str(exc), "server_error") from None
    return change_view(change, path)


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
    return await asyncio.to_thread(_apply, body, config_path())
