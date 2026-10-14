"""Fake engines and helpers for testing the server without models."""

from voiced.testing.fakes import FakeSTTEngine, FakeSTTStreamSession, FakeTTSEngine
from voiced.testing.http import lifespan_client

__all__ = ["FakeSTTEngine", "FakeSTTStreamSession", "FakeTTSEngine", "lifespan_client"]
