"""Per-process service state shared by every transport (HTTP and Wyoming)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from voiced.config import Settings
from voiced.engines.base import EngineNotLoaded, STTEngine, TTSEngine, Voice
from voiced.health import HealthState
from voiced.metrics import Metrics
from voiced.queue import InferenceQueue


@dataclass
class ServiceContext:
    service: Literal["stt", "tts"]
    settings: Settings
    engine: STTEngine | TTSEngine
    queue: InferenceQueue
    health: HealthState
    metrics: Metrics = field(default_factory=Metrics)

    @property
    def stt_engine(self) -> STTEngine:
        assert isinstance(self.engine, STTEngine)
        return self.engine

    @property
    def tts_engine(self) -> TTSEngine:
        assert isinstance(self.engine, TTSEngine)
        return self.engine

    @property
    def languages(self) -> list[str]:
        """The engine's languages that the config allows (``<service>.languages``)."""
        return [code for code in self.engine.languages if _allowed(code, self._configured)]

    async def voices(self) -> list[Voice]:
        """The TTS engine's voices whose language the config allows."""
        return [v for v in await self.tts_engine.voices() if _allowed(v.language, self._configured)]

    def check_loaded(self) -> None:
        """Raise :class:`EngineNotLoaded` until ``load()`` has finished.

        Every transport calls this before taking work. ``classify`` maps the
        error to ``busy``, so clients are told to retry, not that they erred.
        """
        if not self.health.model_loaded:
            raise EngineNotLoaded("model is still loading")

    @property
    def _configured(self) -> list[str]:
        section = self.settings.stt if self.service == "stt" else self.settings.tts
        return section.languages


def _allowed(code: str, allowed: list[str]) -> bool:
    """``en-US`` is allowed by ``en`` or ``en-US`` (case-insensitive)."""
    wanted = {a.lower() for a in allowed}
    code = code.lower()
    return code in wanted or code.split("-")[0] in wanted
