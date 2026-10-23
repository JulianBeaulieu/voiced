"""What the installed engines offer, known without loading them.

Settings UIs need the models and voices an engine offers before (or without)
running it. A TTS engine may declare a ``catalogue_voices()`` classmethod
returning its :class:`~voiced.engines.base.Voice` list; it must not load
weights or import MLX. It is not part of the frozen interface in
``voiced.engines.base``: an engine without it lists no voices, and the
running service's ``voices()`` stays the authority on what can be
synthesized. Any engine may declare an ``unavailable_reason()`` classmethod
the same way, returning why it cannot run on this Mac, as a phrase that
follows the engine's name ("needs macOS 15 or later" for Moonshine below
macOS 15), or None; without it an engine can run. The listing carries it as
``unavailable_reason`` (null: can run), so the app offers neither Download
nor Use, and ``voiced models download`` refuses the engine's models. A hook
that raises fails the whole listing, as ``catalogue_voices`` does.

Models come from ``voiced.models.CATALOGUE`` (the default first), or
``model_id`` for an engine it does not know; languages from ``languages``,
and ``model_languages`` gives each model's own (the catalogue's, else the
engine's), so a settings UI shows the languages of the model in use.

Display labels are built here, in one place: an engine's label is its
registry name capitalised, and a voice with a gender is labelled
``Name (Language, gender)`` (Kokoro's ``af_heart`` is "Heart (American
English, female)"), any other voice by its name. ``voice_languages`` gives
each voice's BCP-47 tag, so a settings UI can group voices without parsing
labels. ``voice_names`` gives the personal name of each voice that has one
(an engine sets ``personal_voice_names = True``), so the app's preview can
say "Hi, I'm Heart." and not "Hi, I'm Middle-aged."

``python -m voiced.engines.catalogue`` prints :func:`engines_list` as JSON.
The watchdog runs it that way, in a child process with a timeout, so engine
plugin code never runs inside the supervisor (ADR 0013).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from typing import Any

from voiced import models
from voiced.engines import registry
from voiced.engines.base import STTEngine, TTSEngine, Voice
from voiced.models.catalogue import engine_label

LANGUAGE_NAMES: dict[str, str] = {
    "en": "English",
    "en-US": "American English",
    "en-GB": "British English",
    "es-ES": "Spanish",
    "fr-FR": "French",
    "hi-IN": "Hindi",
    "it-IT": "Italian",
    "pt-BR": "Brazilian Portuguese",
    "ja-JP": "Japanese",
    "zh-CN": "Mandarin Chinese",
    "de-DE": "German",
    "apc": "Levantine Arabic",
    "fr": "French",
    "de": "German",
}
"""Display names for BCP-47 tags; an unknown tag is shown as is."""


def voice_label(voice: Voice) -> str:
    if voice.gender is None:
        return voice.name
    language = LANGUAGE_NAMES.get(voice.language, voice.language)
    return f"{voice.name} ({language}, {voice.gender})"


def voice_names(cls: type[TTSEngine], voices: list[Voice]) -> dict[str, str]:
    """Each voice's personal name ("Heart"), for a line that names it. Only
    an engine that says ``personal_voice_names = True`` has them; other
    engines' voice names describe the voice ("Middle-aged", "Conversational
    A"), and a "Default" voice names no one."""
    if getattr(cls, "personal_voice_names", False) is not True:
        return {}
    return {v.id: v.name for v in voices if v.name and v.name.lower() != "default"}


def catalogue_voices(cls: type[TTSEngine]) -> list[Voice]:
    declared = getattr(cls, "catalogue_voices", None)
    if not callable(declared):
        return []
    voices = declared()
    return [v for v in voices if isinstance(v, Voice)]


def unavailable_reason(cls: type[STTEngine] | type[TTSEngine]) -> str | None:
    declared = getattr(cls, "unavailable_reason", None)
    if not callable(declared):
        return None
    reason = declared()
    return reason if isinstance(reason, str) and reason else None


def engine_models(name: str, cls: type[STTEngine] | type[TTSEngine]) -> list[str]:
    catalogued = models.engine(name)
    if catalogued is not None:
        return [m.id for m in catalogued.models]
    model = getattr(cls, "model_id", None)
    return [model] if isinstance(model, str) and model else []


def _common(name: str, cls: type[STTEngine] | type[TTSEngine]) -> dict[str, Any]:
    languages = list(getattr(cls, "languages", []))
    catalogued = models.engine(name)
    own = {m.id: list(m.languages) for m in catalogued.models} if catalogued else {}
    return {
        "name": name,
        "label": engine_label(name),
        "models": engine_models(name, cls),
        "languages": languages,
        "model_languages": {m: own.get(m) or languages for m in engine_models(name, cls)},
        "unavailable_reason": unavailable_reason(cls),
    }


def engines_list() -> dict[str, Any]:
    """The ``engines.list`` result from the registry, without loading weights."""
    stt = [_common(name, cls) for name, cls in sorted(registry.list_stt_engines().items())]
    tts: list[dict[str, Any]] = []
    for name, tts_cls in sorted(registry.list_tts_engines().items()):
        voices = catalogue_voices(tts_cls)
        entry = _common(name, tts_cls)
        entry["voices"] = [v.id for v in voices]
        entry["voice_labels"] = {v.id: voice_label(v) for v in voices}
        entry["voice_languages"] = {v.id: v.language for v in voices}
        entry["voice_names"] = voice_names(tts_cls, voices)
        tts.append(entry)
    return {"stt": stt, "tts": tts}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m voiced.engines.catalogue",
        description="Print the engines.list result as JSON.",
    )
    parser.parse_args(argv)
    json.dump(engines_list(), sys.stdout)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
