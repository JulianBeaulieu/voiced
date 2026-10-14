"""The status page (``GET /``) and its data feed (``GET /metrics.json``).

The page is one self-contained HTML file with vanilla JavaScript and a few
Lucide icons inlined as SVG. There is no build step and nothing is fetched
from a CDN at runtime. It polls ``/metrics.json`` which merges the
``/health`` snapshot with the per-operation latency and error metrics
(``voiced.metrics``, which also reports resident memory) and engine
metadata (license, languages). Both routes are
unauthenticated, like ``/health``; the test boxes on the page call the
``/v1`` endpoints and send the API key the operator types in, if one is set.
"""

from __future__ import annotations

from importlib import resources
from typing import Any

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from voiced.api.context import Ctx
from voiced.context import ServiceContext

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
