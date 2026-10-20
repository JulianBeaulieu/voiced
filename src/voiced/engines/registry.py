"""Engine lookup by name.

Engines are discovered through Python entry points, so any installed package
can add one without changes to voiced:

.. code-block:: toml

    [project.entry-points."voiced.stt_engines"]
    whisper = "my_package.whisper:WhisperEngine"

    [project.entry-points."voiced.tts_engines"]
    piper = "my_package.piper:PiperEngine"

The entry point name is what goes in ``engine = "..."`` in the config. Engines
registered in-process with :func:`register_stt_engine` (mainly for tests) take
precedence over entry points of the same name.

:func:`create_engine` builds the engine a config names: the configured
model, and that model's licence from ``voiced.models.CATALOGUE`` (Phonon's
40M models are CC-BY-NC-4.0, its 31M Apache-2.0), so ``/health``,
``/v1/models`` and Wyoming's ``Describe`` name the licence of what is loaded.
"""

from __future__ import annotations

from importlib.metadata import entry_points
from typing import Literal, TypeVar

from voiced import models
from voiced.engines.base import STTEngine, TTSEngine

STT_ENTRY_POINT_GROUP = "voiced.stt_engines"
TTS_ENTRY_POINT_GROUP = "voiced.tts_engines"

_E = TypeVar("_E", STTEngine, TTSEngine)

_local_stt: dict[str, type[STTEngine]] = {}
_local_tts: dict[str, type[TTSEngine]] = {}


class UnknownEngine(LookupError):
    def __init__(self, kind: str, name: str, available: list[str]) -> None:
        choices = ", ".join(sorted(available)) or "none installed"
        super().__init__(f"unknown {kind} engine {name!r}; available: {choices}")
        self.kind = kind
        self.name = name
        self.available = available


def _discover(group: str, base: type[_E]) -> dict[str, type[_E]]:
    found: dict[str, type[_E]] = {}
    for ep in entry_points(group=group):
        cls = ep.load()
        if not (isinstance(cls, type) and issubclass(cls, base)):
            raise TypeError(f"entry point {ep.name!r} in {group} is not a {base.__name__} subclass")
        found[ep.name] = cls
    return found


def list_stt_engines() -> dict[str, type[STTEngine]]:
    """Installed STT engines by name. Entry points are re-read on every call."""
    return {**_discover(STT_ENTRY_POINT_GROUP, STTEngine), **_local_stt}  # type: ignore[type-abstract]


def list_tts_engines() -> dict[str, type[TTSEngine]]:
    """Installed TTS engines by name. Entry points are re-read on every call."""
    return {**_discover(TTS_ENTRY_POINT_GROUP, TTSEngine), **_local_tts}  # type: ignore[type-abstract]


def get_stt_engine(name: str) -> type[STTEngine]:
    engines = list_stt_engines()
    try:
        return engines[name]
    except KeyError:
        raise UnknownEngine("stt", name, list(engines)) from None


def get_tts_engine(name: str) -> type[TTSEngine]:
    engines = list_tts_engines()
    try:
        return engines[name]
    except KeyError:
        raise UnknownEngine("tts", name, list(engines)) from None


def register_stt_engine(name: str, cls: type[STTEngine]) -> None:
    """Register an engine class in this process, shadowing any entry point."""
    _local_stt[name] = cls


def register_tts_engine(name: str, cls: type[TTSEngine]) -> None:
    _local_tts[name] = cls


def create_stt_engine(name: str, model: str) -> STTEngine:
    """An unloaded ``name`` engine for ``model`` (:func:`with_license`).
    Raises :class:`UnknownEngine`, and whatever the engine raises for a
    model it refuses (``ValueError``)."""
    return with_license(get_stt_engine(name)(model))


def create_tts_engine(name: str, model: str) -> TTSEngine:
    return with_license(get_tts_engine(name)(model))


def create_engine(kind: Literal["stt", "tts"], name: str, model: str) -> STTEngine | TTSEngine:
    return create_stt_engine(name, model) if kind == "stt" else create_tts_engine(name, model)


def with_license(engine: _E) -> _E:
    """``engine`` with its model's licence when the catalogue knows the model."""
    license = models.license_of(engine.model_id)
    if license is not None:
        engine.license = license
    return engine
