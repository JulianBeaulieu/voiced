"""Loose enforcement of the OpenAI ``model`` request field.

OpenAI clients always send a model name (``whisper-1``, ``tts-1``) and it is
ignored: each voiced process serves exactly one model. The one case that is
an error is naming the model (or engine name) of *another installed engine*,
because the caller clearly expected a different model than the one running
here. This is HTTP request validation, not inference policy (ADR 0005), so it
lives in ``voiced.api`` and raises :class:`APIError`.

The map of foreign names is built once per app in ``voiced.api.app`` and
kept on ``app.state.foreign_models``; scanning entry points per request would
import third-party engine packages on the request path.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated

from fastapi import Depends, status
from starlette.requests import HTTPConnection

from voiced.api.context import APIError
from voiced.context import ServiceContext
from voiced.engines.base import STTEngine, TTSEngine


def foreign_model_map(
    engines: Mapping[str, type[STTEngine] | type[TTSEngine]], own: str
) -> dict[str, str]:
    """``{model_id_or_name: engine_name}`` for every installed engine except ``own``."""
    out: dict[str, str] = {}
    for name, cls in engines.items():
        if name == own:
            continue
        out[name] = name
        model_id = getattr(cls, "model_id", None)
        if isinstance(model_id, str):
            out[model_id] = name
    return out


def get_foreign_models(conn: HTTPConnection) -> dict[str, str]:
    found: dict[str, str] = getattr(conn.app.state, "foreign_models", {})
    return found


ForeignModels = Annotated[dict[str, str], Depends(get_foreign_models)]


def reject_foreign_model(
    ctx: ServiceContext, foreign: Mapping[str, str], model: str | None
) -> None:
    """400 ``model_not_served`` if ``model`` names another installed engine's model."""
    if not model or model == ctx.engine.model_id or model == ctx.engine.name:
        return
    owner = foreign.get(model)
    if owner is not None:
        raise APIError(
            status.HTTP_400_BAD_REQUEST,
            f"model {model!r} belongs to the {owner!r} engine; this service runs "
            f"{ctx.engine.name!r} ({ctx.engine.model_id}). Change the config to switch.",
            type="model_not_served",
        )
