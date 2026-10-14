"""HTTP test client that runs the app's lifespan (engine load) like uvicorn does."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from fastapi import FastAPI

if TYPE_CHECKING:
    from httpx import AsyncClient


@asynccontextmanager
async def lifespan_client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    """``async with lifespan_client(app) as client:`` with the engine loaded.

    httpx's ``ASGITransport`` does not trigger startup and shutdown events, so
    this drives the lifespan by hand. Requires ``httpx`` (a dev dependency).
    """
    from httpx import ASGITransport, AsyncClient

    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://voiced") as c:
            yield c
