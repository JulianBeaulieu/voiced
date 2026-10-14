"""Kokoro-82M on MLX, with espeak-ng phonemes.

Pronunciation path (see ``docs/decisions/0004-tts-kokoro-espeak-tokens.md``):
text is normalized, split into sentences, phonemized with espeak-ng through
``kokoro_onnx.tokenizer.Tokenizer``, and the phoneme string is fed straight to
the MLX model together with the voice's style vector. mlx-audio's own
pipeline (misaki G2P) is bypassed entirely.

espeak-ng is an external, GPL-3 program. It is never vendored: the engine
looks for the Homebrew installation first, then an explicit path from the
environment, then the copy bundled with the ``espeakng-loader`` wheel, and
fails at ``load()`` with the install command if none works.

Everything that touches MLX or espeak is imported inside ``load()`` so this
module can be imported (the entry point registry does so) on machines
without the ``tts`` extra.
"""

from __future__ import annotations

import asyncio
import ctypes.util
import logging
import os
import time
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from voiced.engines.audio import to_pcm16
from voiced.engines.base import (
    TTS_SAMPLE_RATE,
    EngineError,
    EngineNotLoaded,
    PCMChunk,
    TTSEngine,
    UnknownVoice,
    Voice,
)
from voiced.tts.normalize import normalize
from voiced.tts.sentences import split_sentences

log = logging.getLogger(__name__)

__all__ = [
    "ESPEAK_INSTALL_HINT",
    "EspeakNotFound",
    "KokoroEngine",
    "KokoroLanguage",
    "find_espeak",
    "kokoro_voices",
]

ESPEAK_INSTALL_HINT = "brew install espeak-ng"
ENV_ESPEAK_LIBRARY = "VOICED_ESPEAK_LIBRARY"
ENV_ESPEAK_DATA = "VOICED_ESPEAK_DATA"
MAX_PHONEMES = 510
"""Kokoro's context is 512 tokens including the two padding tokens."""
WARMUP_TEXT = "Warming up."


class EspeakNotFound(RuntimeError):
    """espeak-ng could not be found or does not work. Not a client error."""


@dataclass(frozen=True, slots=True)
class KokoroLanguage:
    """One Kokoro language: the voice-id prefix letter, its BCP-47 tag and the
    espeak-ng voice that phonemizes it."""

    prefix: str
    tag: str
    espeak: str


# Every language Kokoro-82M ships voices for, keyed by the first letter of
# the voice id. Only those whose ``tag`` is in ``KokoroEngine.languages`` are
# exposed; enabling another one is a change to that list (and a check that
# espeak-ng's phonemes for it fit the model's training data, which is what
# misaki provides for ja and zh).
KOKORO_LANGUAGES: dict[str, KokoroLanguage] = {
    "a": KokoroLanguage("a", "en-US", "en-us"),
    "b": KokoroLanguage("b", "en-GB", "en-gb"),
    "e": KokoroLanguage("e", "es-ES", "es"),
    "f": KokoroLanguage("f", "fr-FR", "fr-fr"),
    "h": KokoroLanguage("h", "hi-IN", "hi"),
    "i": KokoroLanguage("i", "it-IT", "it"),
    "p": KokoroLanguage("p", "pt-BR", "pt-br"),
    "j": KokoroLanguage("j", "ja-JP", "ja"),
    "z": KokoroLanguage("z", "zh-CN", "cmn"),
}

# Voice ids in the mlx-community/Kokoro-82M-bf16 repo. The second letter is
# the gender. Display names are the capitalised suffix.
KOKORO_VOICE_IDS: tuple[str, ...] = (
    "af_alloy", "af_aoede", "af_bella", "af_heart", "af_jessica", "af_kore",
    "af_nicole", "af_nova", "af_river", "af_sarah", "af_sky",
    "am_adam", "am_echo", "am_eric", "am_fenrir", "am_liam", "am_michael",
    "am_onyx", "am_puck", "am_santa",
    "bf_alice", "bf_emma", "bf_isabella", "bf_lily",
    "bm_daniel", "bm_fable", "bm_george", "bm_lewis",
    "ef_dora", "em_alex", "em_santa",
    "ff_siwis",
    "hf_alpha", "hf_beta", "hm_omega", "hm_psi",
    "if_sara", "im_nicola",
    "jf_alpha", "jf_gongitsune", "jf_nezumi", "jf_tebukuro", "jm_kumo",
    "pf_dora", "pm_alex", "pm_santa",
    "zf_xiaobei", "zf_xiaoni", "zf_xiaoxiao", "zf_xiaoyi",
    "zm_yunjian", "zm_yunxi", "zm_yunxia", "zm_yunyang",
)  # fmt: skip

_GENDERS: dict[str, Literal["female", "male"]] = {"f": "female", "m": "male"}


def kokoro_voices(languages: list[str]) -> list[Voice]:
    """The voice table filtered to ``languages`` (BCP-47 tags)."""
    enabled = {lang.prefix for lang in KOKORO_LANGUAGES.values() if lang.tag in languages}
    out: list[Voice] = []
    for voice_id in KOKORO_VOICE_IDS:
        prefix, _, name = voice_id.partition("_")
        if prefix[0] not in enabled:
            continue
        out.append(
            Voice(
                id=voice_id,
                name=name.capitalize(),
                language=KOKORO_LANGUAGES[prefix[0]].tag,
                gender=_GENDERS.get(prefix[1]),
            )
        )
    return out


def find_espeak(
    library: str | None = None, data: str | None = None
) -> list[tuple[str, str | None]]:
    """Candidate ``(library_path, data_path)`` pairs, most preferred first.

    Explicit arguments win, then ``VOICED_ESPEAK_LIBRARY`` /
    ``VOICED_ESPEAK_DATA``, then the Homebrew keg, then whatever the dynamic
    linker finds, then the copy inside the ``espeakng-loader`` wheel. Each
    candidate is only listed if the library file exists.
    """
    candidates: list[tuple[str, str | None]] = []
    if library:
        candidates.append((library, data))
    env_lib = os.environ.get(ENV_ESPEAK_LIBRARY) or os.environ.get("PHONEMIZER_ESPEAK_LIBRARY")
    if env_lib:
        env_data = os.environ.get(ENV_ESPEAK_DATA) or os.environ.get("PHONEMIZER_ESPEAK_DATA_PATH")
        candidates.append((env_lib, env_data))
    for prefix in ("/opt/homebrew/opt/espeak-ng", "/usr/local/opt/espeak-ng"):
        candidates.append((f"{prefix}/lib/libespeak-ng.dylib", f"{prefix}/share/espeak-ng-data"))
    found = ctypes.util.find_library("espeak-ng")
    if found:
        candidates.append((found, None))
    try:
        import espeakng_loader

        candidates.append((espeakng_loader.get_library_path(), espeakng_loader.get_data_path()))
    except Exception:
        pass
    return [(lib, data) for lib, data in candidates if Path(lib).is_file()]


class KokoroEngine(TTSEngine):
    name = "kokoro"
    model_id = "mlx-community/Kokoro-82M-bf16"
    languages = ["en-US", "en-GB"]
    license = "Apache-2.0"

    def __init__(
        self,
        model_id: str | None = None,
        *,
        espeak_library: str | None = None,
        espeak_data: str | None = None,
        warmup: bool = True,
    ) -> None:
        super().__init__(model_id)
        self._espeak_library = espeak_library
        self._espeak_data = espeak_data
        self._warmup = warmup
        self._model: Any = None
        self._tokenizer: Any = None
        self._packs: dict[str, Any] = {}
        self._voices: list[Voice] = kokoro_voices(self.languages)
        self._voice_by_id = {v.id: v for v in self._voices}

    # -- lifecycle -----------------------------------------------------------

    async def load(self) -> None:
        await asyncio.to_thread(self._load_sync)

    def _load_sync(self) -> None:
        started = time.perf_counter()
        self._tokenizer = self._load_espeak()
        import mlx.core as mx
        from mlx_audio.tts.utils import load_model

        self._model = load_model(self.model_id)
        rate = int(self._model.sample_rate)
        if rate != TTS_SAMPLE_RATE:
            raise RuntimeError(f"Kokoro model reports {rate} Hz, expected {TTS_SAMPLE_RATE}")
        self._packs = self._load_voice_packs()
        missing = [v.id for v in self._voices if v.id not in self._packs]
        if missing:
            self._voices = [v for v in self._voices if v.id in self._packs]
            self._voice_by_id = {v.id: v for v in self._voices}
            log.warning(
                "kokoro: %d voices missing from %s: %s", len(missing), self.model_id, missing
            )
        if not self._voices:
            raise RuntimeError(f"no usable voices found in {self.model_id}")
        mx.eval(*self._packs.values())
        if self._warmup:
            # One sentence per enabled language so the first real request of
            # either kind does not pay for graph construction.
            for lang in self._enabled_languages():
                voice = next(v.id for v in self._voices if v.language == lang.tag)
                self._synth_sentence(WARMUP_TEXT, voice, 1.0)
        log.info(
            "kokoro: loaded %s with %d voices in %.1fs",
            self.model_id,
            len(self._voices),
            time.perf_counter() - started,
        )

    def _load_espeak(self) -> Any:
        from kokoro_onnx.config import EspeakConfig
        from kokoro_onnx.tokenizer import Tokenizer

        errors: list[str] = []
        for library, data in find_espeak(self._espeak_library, self._espeak_data):
            try:
                tokenizer = Tokenizer(EspeakConfig(lib_path=library, data_path=data))
                probe = tokenizer.phonemize("hello", "en-us")
            except Exception as exc:
                errors.append(f"{library}: {type(exc).__name__}: {exc}")
                continue
            if not probe:
                errors.append(f"{library}: phonemizer returned nothing (data path {data!r})")
                continue
            log.info("kokoro: espeak-ng at %s", library)
            return tokenizer
        detail = "; ".join(errors) if errors else "no espeak-ng library found"
        raise EspeakNotFound(
            "Kokoro needs espeak-ng for pronunciation but it is not installed or not "
            f"working ({detail}). Install it with `{ESPEAK_INSTALL_HINT}`, or point "
            f"{ENV_ESPEAK_LIBRARY} and {ENV_ESPEAK_DATA} at a working copy."
        )

    def _load_voice_packs(self) -> dict[str, Any]:
        from mlx_audio.tts.models.kokoro.voice import load_voice_tensor

        root = Path(self.model_id)
        if not root.is_dir():
            from huggingface_hub import snapshot_download

            root = Path(snapshot_download(self.model_id, allow_patterns=["voices/*.safetensors"]))
        packs: dict[str, Any] = {}
        for voice in self._voices:
            path = root / "voices" / f"{voice.id}.safetensors"
            if path.is_file():
                packs[voice.id] = load_voice_tensor(str(path))
        return packs

    def _enabled_languages(self) -> list[KokoroLanguage]:
        return [lang for lang in KOKORO_LANGUAGES.values() if lang.tag in self.languages]

    # -- interface -----------------------------------------------------------

    async def voices(self) -> list[Voice]:
        self._check_loaded()
        return list(self._voices)

    async def synthesize(
        self, text: str, voice: str, speed: float = 1.0
    ) -> AsyncIterator[PCMChunk]:
        self._check_loaded()
        if voice not in self._voice_by_id:
            raise UnknownVoice(voice)
        if not speed > 0:
            raise EngineError("speed must be positive")
        for sentence in split_sentences(normalize(text)):
            pcm = await asyncio.to_thread(self._synth_sentence, sentence, voice, speed)
            if pcm:
                yield PCMChunk(data=pcm, sample_rate=TTS_SAMPLE_RATE)

    # -- synthesis (blocking, runs in a worker thread) -----------------------

    def phonemize(self, text: str, voice: str) -> str:
        """espeak-ng phonemes for ``text`` in the language of ``voice``."""
        self._check_loaded()
        lang = KOKORO_LANGUAGES[voice[0]].espeak
        return str(self._tokenizer.phonemize(text, lang))

    def _synth_sentence(self, sentence: str, voice: str, speed: float) -> bytes:
        import numpy as np

        phonemes = self.phonemize(sentence, voice)
        if not phonemes:
            return b""
        pack = self._packs[voice]
        parts: list[Any] = []
        for chunk in _chunk_phonemes(phonemes, MAX_PHONEMES):
            out = self._model(chunk, pack[len(chunk) - 1], speed, return_output=True)
            parts.append(np.array(out.audio, copy=False).reshape(-1))
        audio = np.concatenate(parts) if len(parts) > 1 else parts[0]
        return to_pcm16(audio, TTS_SAMPLE_RATE)

    def _check_loaded(self) -> None:
        if self._model is None or self._tokenizer is None:
            raise EngineNotLoaded(self.name)


def _chunk_phonemes(phonemes: str, limit: int) -> Iterator[str]:
    """Split a phoneme string at spaces so no piece exceeds ``limit``. The
    sentence splitter keeps this from happening for normal text; it is a
    guard for a 300-character sentence with no spaces at all."""
    while len(phonemes) > limit:
        cut = phonemes.rfind(" ", 1, limit + 1)
        if cut <= 0:
            cut = limit
        head, phonemes = phonemes[:cut].strip(), phonemes[cut:].strip()
        if head:
            yield head
    if phonemes:
        yield phonemes
