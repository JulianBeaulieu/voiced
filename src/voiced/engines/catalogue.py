"""What an engine offers, known without loading it.

Settings UIs need the models and voices an engine offers before (or without)
running it. Engines may declare a ``catalogue()`` classmethod returning an
:class:`EngineCatalogue`; it must not load weights or import MLX. It is not
part of the frozen interface in ``voiced.engines.base``: engines without it
are described by their class attributes alone, and the running service's
``voices()`` stays the authority on what can be synthesized.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from voiced.engines.base import STTEngine, TTSEngine


@dataclass(frozen=True)
class EngineCatalogue:
    models: list[str]
    languages: list[str]
    voices: list[str] = field(default_factory=list)


def catalogue_of(cls: type[STTEngine] | type[TTSEngine]) -> EngineCatalogue:
    """``cls.catalogue()`` if the engine declares one, else its class attributes."""
    declared = getattr(cls, "catalogue", None)
    if callable(declared):
        result = declared()
        if isinstance(result, EngineCatalogue):
            return result
    return EngineCatalogue(
        models=[cls.model_id] if hasattr(cls, "model_id") else [],
        languages=list(getattr(cls, "languages", [])),
    )
