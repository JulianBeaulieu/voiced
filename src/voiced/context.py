"""Per-process service state shared by every transport (HTTP and Wyoming)."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Literal

from voiced.config import Settings
from voiced.engines.base import (
    EngineNotLoaded,
    STTEngine,
    TTSEngine,
    UnknownVoice,
    UnsupportedLanguage,
    Voice,
)
from voiced.health import HealthResponse, HealthState
from voiced.metrics import Metrics
from voiced.models import ModelNotDownloaded
from voiced.queue import Admission, InferenceQueue
from voiced.residency import Residency
from voiced.settings_store import LiveSettings
from voiced.text import primary_subtag

log = logging.getLogger(__name__)


@dataclass
class ServiceContext:
    service: Literal["stt", "tts"]
    settings: Settings
    engine: STTEngine | TTSEngine
    queue: InferenceQueue
    health: HealthState
    metrics: Metrics = field(default_factory=Metrics)
    uploads: Admission = field(init=False)
    """Uploads being decoded before they queue for the slot (HTTP STT); as
    many at once as the queue holds."""
    residency: Residency = field(init=False)
    """Every load and unload of the engine (ADR 0033)."""
    engine_voices: list[Voice] = field(default_factory=list)
    """A TTS engine's voices, kept from its first load so they are listed
    while the model is unloaded (``voices()`` is stable after a load)."""
    live: LiveSettings = field(default_factory=LiveSettings)
    """The config file now, for the settings read per request."""

    def __post_init__(self) -> None:
        self.uploads = Admission(self.queue.capacity)
        idle_unload_s = self.settings.service(self.service).idle_unload_s
        self.residency = Residency(self.engine, self.health, self.queue, idle_unload_s)

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
                if primary_subtag(configured) == primary_subtag(engine_tag):
                    tag = _norm(max(configured, engine_tag, key=_depth))
                    if tag.lower() not in {t.lower() for t in out}:
                        out.append(tag)
        return out

    async def voices(self) -> list[Voice]:
        """The TTS engine's voices whose language matches :attr:`languages`;
        as of its first load, so they are listed while the model is unloaded."""
        bases = {primary_subtag(t) for t in self.languages}
        voices = self.engine_voices or await self.tts_engine.voices()
        return [v for v in voices if primary_subtag(v.language) in bases]

    def default_speed(self) -> float:
        """``tts.speed`` as the config file has it now, for a request that
        names no speed: it is read per request, so a change applies without a
        restart (ADR 0013). While the file cannot be read, the last good
        reading, else the speed the service started with."""
        live = self.live.get() or self.live.last or self.settings
        return live.tts.speed

    def check_language(self, language: str | None) -> str | None:
        """Resolve a client's language hint to the engine's own code.

        ``None`` (auto-detect or engine default) passes through. Otherwise the
        hint must match one of :attr:`languages` by primary subtag. The engine
        code returned prefers, in order: an exact tag match, the engine tag
        the service advertises for that subtag, then any tag with that subtag.
        So with an ``["en-US", "en-GB"]`` engine, ``en-GB`` resolves to
        ``en-GB``, not ``en-US``. Raises :class:`UnsupportedLanguage` (an
        ``EngineError``) if nothing matches.
        """
        if language is None:
            return None
        want = _norm(language).lower()
        base = primary_subtag(language)
        if base not in {primary_subtag(t) for t in self.languages}:
            raise UnsupportedLanguage(f"language {language!r} is not served; try {self.languages}")
        engine_tags = [t for t in self.engine.languages if primary_subtag(t) == base]
        for tag in engine_tags:
            if _norm(tag).lower() == want:  # exact match
                return tag
        advertised = {_norm(t).lower() for t in self.languages if primary_subtag(t) == base}
        for tag in engine_tags:
            if _norm(tag).lower() in advertised:  # the tag we advertise for this base
                return tag
        return engine_tags[0]

    async def check_voice(self, voice: str | None, *, listed_by: str) -> Voice:
        """Resolve a client's voice to the :class:`Voice` this service serves.

        ``None`` means ``tts.default_voice``. A voice that is not one of
        :meth:`voices` (the engine's voices in the configured languages: an
        engine may know more, but those are not served) falls back to
        ``tts.default_voice``, or the first served voice, with a warning:
        Home Assistant keeps sending its pipeline's voice after the engine
        changes, and refusing it would leave the assistant silent (ADR
        0022). Raises :class:`UnknownVoice` (an ``EngineError``) naming
        ``listed_by`` only when no voice is served at all.
        """
        served = await self.voices()
        chosen = voice or self.settings.tts.default_voice
        for candidate in served:
            if candidate.id == chosen:
                return candidate
        if not served:
            source = "" if voice else " (tts.default_voice)"
            raise UnknownVoice(f"voice {chosen!r}{source} is not served; see {listed_by}")
        default = self.settings.tts.default_voice
        fallback = next((v for v in served if v.id == default), served[0])
        log.warning(
            "voice %r is not served by %s; using %r (see %s)",
            chosen,
            self.engine.name,
            fallback.id,
            listed_by,
        )
        return fallback

    def check_loaded(self) -> None:
        """Raise :class:`EngineNotLoaded` until the first ``load()`` has
        finished, and :class:`ModelNotDownloaded` if a load found the model
        missing. A model unloaded while idle passes: ``inference()`` loads it
        again (ADR 0033).

        Every transport calls this before taking work. ``classify`` maps both
        to ``busy``, so clients are told to retry, not that they erred; HTTP
        answers a missing model with ``model_not_downloaded`` and no
        ``Retry-After``, since retrying will not help until it is downloaded.
        """
        missing = self.health.missing
        if missing is not None:
            # A new exception each time: raising the stored one would grow
            # its traceback with every request.
            raise ModelNotDownloaded(missing.model_id, missing.revision, missing.missing)
        if not self.health.started:
            raise EngineNotLoaded("model is still loading")

    def public_health(self) -> HealthResponse:
        """The ``/health`` snapshot as unauthenticated routes show it.

        With ``server.api_key`` set, ``last_error`` keeps only the exception
        type: its message can echo what a client sent, and these routes are
        open to anyone who can reach the port (the same rule as the error log
        in ``/metrics.json``).
        """
        snapshot = self.health.snapshot(self.queue.depth)
        if self.settings.server.api_key and snapshot.last_error:
            snapshot.last_error = f"{self.health.last_error_type} (details in the service log)"
        return snapshot

    @property
    def _configured(self) -> list[str]:
        section = self.settings.stt if self.service == "stt" else self.settings.tts
        return section.languages


def _norm(tag: str) -> str:
    return tag.replace("_", "-")


def _depth(tag: str) -> int:
    return len(_norm(tag).split("-"))
