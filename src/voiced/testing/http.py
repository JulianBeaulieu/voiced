"""HTTP test client that runs the app's lifespan (engine load) like uvicorn does."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from fastapi import FastAPI

if TYPE_CHECKING:
    from httpx import AsyncClient


@asynccontextmanager
async def lifespan_client(
    app: FastAPI, *, wait_loaded: bool = True, raise_app_exceptions: bool = True
) -> AsyncIterator[AsyncClient]:
    """``async with lifespan_client(app) as client:`` with the engine loaded.

    httpx's ``ASGITransport`` does not trigger startup and shutdown events, so
    this drives the lifespan by hand. The engine loads in the background (see
    ``voiced.api.app``); ``wait_loaded=False`` yields while it is still
    loading. Requires ``httpx`` (a dev dependency).
    """
    from httpx import ASGITransport, AsyncClient

    async with app.router.lifespan_context(app):
        if wait_loaded:
            await app.state.load_task
        transport = ASGITransport(app=app, raise_app_exceptions=raise_app_exceptions)
        async with AsyncClient(transport=transport, base_url="http://voiced") as c:
            yield c
