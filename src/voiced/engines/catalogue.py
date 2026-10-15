"""What the installed engines offer, known without loading them.

Settings UIs need the models and voices an engine offers before (or without)
running it. A TTS engine may declare a ``catalogue_voices()`` classmethod
returning its :class:`~voiced.engines.base.Voice` list; it must not load
weights or import MLX. It is not part of the frozen interface in
``voiced.engines.base``: an engine without it lists no voices, and the
running service's ``voices()`` stays the authority on what can be
synthesized. Models and languages come from ``model_id`` and ``languages``.

Display labels are built here, in one place: an engine's label is its
registry name capitalised, and a voice with a gender is labelled
``Name (Language, gender)`` (Kokoro's ``af_heart`` is "Heart (American
English, female)"), any other voice by its name.

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

from voiced.engines import registry
from voiced.engines.base import STTEngine, TTSEngine, Voice

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
}
"""Display names for BCP-47 tags; an unknown tag is shown as is."""


def engine_label(name: str) -> str:
    return name[:1].upper() + name[1:]


def voice_label(voice: Voice) -> str:
    if voice.gender is None:
        return voice.name
    language = LANGUAGE_NAMES.get(voice.language, voice.language)
    return f"{voice.name} ({language}, {voice.gender})"


def catalogue_voices(cls: type[TTSEngine]) -> list[Voice]:
    declared = getattr(cls, "catalogue_voices", None)
    if not callable(declared):
        return []
    voices = declared()
    return [v for v in voices if isinstance(v, Voice)]


def _common(name: str, cls: type[STTEngine] | type[TTSEngine]) -> dict[str, Any]:
    model = getattr(cls, "model_id", None)
    return {
        "name": name,
        "label": engine_label(name),
        "models": [model] if isinstance(model, str) and model else [],
        "languages": list(getattr(cls, "languages", [])),
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
