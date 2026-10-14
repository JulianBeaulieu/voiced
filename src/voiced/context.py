"""Per-process service state shared by every transport (HTTP and Wyoming)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from voiced.config import Settings
from voiced.engines.base import (
    EngineNotLoaded,
    STTEngine,
    TTSEngine,
    UnsupportedLanguage,
    Voice,
)
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
        """Languages this service serves: engine languages the config allows.

        Tags match on their primary subtag in either direction (``en``,
        ``en-US`` and ``en_GB`` all match), and the narrower tag of each
        matching pair is returned, so ``tts.languages = ["en-gb"]`` with an
        ``en`` engine advertises ``en-gb``. Empty only if no voice is served.
        """
        out: list[str] = []
        for configured in self._configured:
            for engine_tag in self.engine.languages:
                if _base(configured) == _base(engine_tag):
                    tag = _norm(max(configured, engine_tag, key=_depth))
                    if tag.lower() not in {t.lower() for t in out}:
                        out.append(tag)
        return out

    async def voices(self) -> list[Voice]:
        """The TTS engine's voices whose language matches :attr:`languages`."""
        bases = {_base(t) for t in self.languages}
        return [v for v in await self.tts_engine.voices() if _base(v.language) in bases]

    def check_language(self, language: str | None) -> str | None:
        """Resolve a client's language hint to the engine's own code.

        ``None`` (auto-detect or engine default) passes through. Otherwise the
        hint must match one of :attr:`languages` by primary subtag; the
        matching entry of ``engine.languages`` is returned. Raises
        :class:`UnsupportedLanguage` (an ``EngineError``) if nothing matches.
        """
        if language is None:
            return None
        if _base(language) in {_base(t) for t in self.languages}:
            for engine_tag in self.engine.languages:
                if _base(engine_tag) == _base(language):
                    return engine_tag
        raise UnsupportedLanguage(f"language {language!r} is not served; try {self.languages}")

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


def _norm(tag: str) -> str:
    return tag.replace("_", "-")


def _base(tag: str) -> str:
    return _norm(tag).split("-")[0].lower()


def _depth(tag: str) -> int:
    return len(_norm(tag).split("-"))
