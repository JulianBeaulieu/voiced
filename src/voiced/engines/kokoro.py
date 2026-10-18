"""Kokoro-82M on MLX, with espeak-ng phonemes.

Pronunciation path (see ``docs/decisions/0009-tts-kokoro-espeak-tokens.md``):
the already normalized text is split into sentences, each sentence is
phonemized with espeak-ng through ``kokoro_onnx.tokenizer.Tokenizer``, and
the phoneme string is fed straight to the MLX model together with the
voice's style vector. mlx-audio's own pipeline (misaki G2P) is bypassed.

espeak-ng is an external, GPL-3 program and is never vendored. The engine
uses the Homebrew installation (``brew install espeak-ng``), or the library
and data directory named by ``VOICED_ESPEAK_LIBRARY`` and
``VOICED_ESPEAK_DATA`` when those are set (the services set them from
``[tts] espeak_library`` and ``espeak_data``), and fails at ``load()`` with the
install command if neither works. When no data directory is named, the
tokenizer reads the data of the espeak-ng copy in the ``espeakng-loader``
wheel; that copy is cloned to a short path first (``_espeak_data``).

Everything that touches MLX or espeak is imported inside ``load()`` so this
module can be imported (the entry point registry does so) on machines
without the ``tts`` extra.
"""

from __future__ import annotations

import importlib.metadata
import logging
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import numpy as np
from numpy.typing import NDArray

from voiced.audio import float32_to_pcm16
from voiced.config import ENV_ESPEAK_DATA, ENV_ESPEAK_LIBRARY
from voiced.engines.base import TTS_SAMPLE_RATE, Voice
from voiced.engines.sentence import WARMUP_TEXT, SentenceTTSEngine
from voiced.text.sentences import cut_to_length

log = logging.getLogger(__name__)

__all__ = [
    "ENV_ESPEAK_DATA",
    "ENV_ESPEAK_LIBRARY",
    "ESPEAK_INSTALL_HINT",
    "EspeakNotFound",
    "KokoroEngine",
    "KokoroLanguage",
    "espeak_candidates",
    "kokoro_voices",
]

ESPEAK_INSTALL_HINT = "brew install espeak-ng"
HOMEBREW_PREFIXES = ("/opt/homebrew/opt/espeak-ng", "/usr/local/opt/espeak-ng")
MAX_ESPEAK_DATA_PATH = 159
"""Bytes. The espeak-ng in the ``espeakng-loader`` wheel keeps its data path in
a 160-byte buffer; a longer path makes it print ``Error processing file`` and
call ``exit(1)``, which takes the whole process down. Homebrew's build has no
such limit."""
_CS_DARWIN_USER_CACHE_DIR = 65538
"""``confstr`` name for the per-user cache directory under ``/var/folders``."""
_STALE_STAGING_SECONDS = 3600
MAX_PHONEMES = 510
"""Kokoro's context is 512 tokens including the two padding tokens."""


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
# exposed. Enabling another one also needs a text normalizer for it
# (``voiced.text.normalize`` is English only) and a check that espeak-ng's
# phonemes for it fit the model's training data (misaki uses other G2Ps for
# ja and zh).
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


def espeak_candidates() -> list[tuple[str, str | None]]:
    """``(library_path, data_path)`` pairs to try, most preferred first.

    ``VOICED_ESPEAK_LIBRARY`` (with optional ``VOICED_ESPEAK_DATA``), which
    ``voiced`` sets from ``[tts] espeak_library`` and ``espeak_data``, is the one
    override and is the only candidate when set; a path that does not exist
    is an error naming it. Otherwise the Homebrew keg for Apple Silicon and
    for Intel, whichever exists.
    """
    override = os.environ.get(ENV_ESPEAK_LIBRARY)
    if override:
        data = os.environ.get(ENV_ESPEAK_DATA) or None
        if not Path(override).is_file():
            raise EspeakNotFound(f"[tts] espeak_library {override} is not a file")
        if data and not Path(data).is_dir():
            raise EspeakNotFound(f"[tts] espeak_data {data} is not a directory")
        return [(override, data)]
    return [
        (f"{prefix}/lib/libespeak-ng.dylib", f"{prefix}/share/espeak-ng-data")
        for prefix in HOMEBREW_PREFIXES
        if Path(f"{prefix}/lib/libespeak-ng.dylib").is_file()
    ]


class KokoroEngine(SentenceTTSEngine):
    name = "kokoro"
    model_id = "mlx-community/Kokoro-82M-bf16"
    languages = ["en-US", "en-GB"]
    license = "Apache-2.0"

    @classmethod
    def catalogue_voices(cls) -> list[Voice]:
        """The static voice table for ``languages``; no weights are read
        (``voiced.engines.catalogue``)."""
        return kokoro_voices(cls.languages)

    def __init__(self, model_id: str | None = None, *, warmup: bool = True) -> None:
        super().__init__(model_id)
        self._warmup = warmup
        self._tokenizer: Any = None
        self._packs: dict[str, Any] = {}
        self._voices: list[Voice] = []

    # -- lifecycle -----------------------------------------------------------

    def _load_sync(self) -> None:
        """Build everything in locals and assign at the end, so a failure
        part-way leaves the engine unloaded rather than half loaded."""
        started = time.perf_counter()
        tokenizer = _load_espeak()
        import mlx.core as mx
        from mlx_audio.tts.utils import load_model

        model = load_model(self.model_id)
        rate = int(model.sample_rate)
        if rate != TTS_SAMPLE_RATE:
            raise RuntimeError(f"Kokoro model reports {rate} Hz, expected {TTS_SAMPLE_RATE}")
        all_voices = kokoro_voices(self.languages)
        packs = self._load_voice_packs(all_voices)
        voices = [v for v in all_voices if v.id in packs]
        if not voices:
            raise RuntimeError(f"no usable voices found in {self.model_id}")
        mx.eval(*packs.values())
        if self._warmup:
            # One sentence per enabled language so the first real request of
            # either kind does not pay for graph construction.
            for tag in self.languages:
                voice = next((v.id for v in voices if v.language == tag), None)
                if voice is not None:
                    _synth(model, tokenizer, packs[voice], WARMUP_TEXT, voice, 1.0)
        self._tokenizer = tokenizer
        self._packs = packs
        self._voices = voices
        self._model = model  # last: it is what marks the engine loaded
        log.info(
            "kokoro: loaded %s with %d voices in %.1fs",
            self.model_id,
            len(voices),
            time.perf_counter() - started,
        )

    def _load_voice_packs(self, voices: list[Voice]) -> dict[str, Any]:
        from mlx_audio.tts.models.kokoro.voice import load_voice_tensor

        root = Path(self.model_id)
        if not root.is_dir():
            from huggingface_hub import snapshot_download

            root = Path(snapshot_download(self.model_id, allow_patterns=["voices/*.safetensors"]))
        packs: dict[str, Any] = {}
        missing: list[str] = []
        for voice in voices:
            path = root / "voices" / f"{voice.id}.safetensors"
            if path.is_file():
                packs[voice.id] = load_voice_tensor(str(path))
            else:
                missing.append(voice.id)
        if missing:
            log.warning("kokoro: %d voices missing from %s: %s", len(missing), root, missing)
        return packs

    # -- interface -----------------------------------------------------------

    async def voices(self) -> list[Voice]:
        self._check_loaded()
        return list(self._voices)

    def _has_voice(self, voice: str) -> bool:
        return voice in self._packs

    def phonemize(self, text: str, voice: str) -> str:
        """espeak-ng phonemes for ``text`` in the language of ``voice``.

        For tests and diagnostics only: it runs the phonemizer on the calling
        thread, and espeak-ng holds process-global state that is not safe to
        call concurrently with a synthesis in flight. Synthesis itself
        phonemizes inside the worker thread the slot already serializes.
        """
        self._check_loaded()
        return _phonemize(self._tokenizer, text, voice)

    def _synth_sentence(self, sentence: str, voice: str, speed: float) -> bytes:
        """Blocking; runs in a worker thread."""
        return _synth(self._model, self._tokenizer, self._packs[voice], sentence, voice, speed)


# -- blocking helpers ----------------------------------------------------------


def _bundled_espeak() -> tuple[str, str] | None:
    """``(data, version)`` of the espeak-ng in the ``espeakng-loader`` wheel,
    or ``None`` when it is not installed."""
    try:
        import espeakng_loader
    except ImportError:
        return None
    try:
        version = importlib.metadata.version("espeakng-loader")
    except importlib.metadata.PackageNotFoundError:
        version = "unknown"
    return str(espeakng_loader.get_data_path()), version


def _user_cache_dir() -> Path | None:
    """The per-user cache directory (``/private/var/folders/../C``), resolved,
    or ``None`` when the system has none."""
    try:
        cache = os.confstr(_CS_DARWIN_USER_CACHE_DIR)
    except (ValueError, OSError):
        return None
    return Path(os.path.realpath(cache)) if cache else None


def _short_espeak_copy(source: str, version: str) -> str:
    """Clone the espeak-ng data directory ``source`` to
    ``<user cache dir>/voiced/espeak-ng-data-<version>`` and return that path.

    ``cp -c`` makes an APFS clone, so the copy takes no extra disk space; a
    plain copy is the fallback on other file systems. A complete copy (it has
    ``phontab``) is reused and never replaced, so a running service never sees
    its data change under it; a wheel upgrade gets a new directory, and one
    the system purged is made again. Concurrent loads each clone into their
    own staging directory and the first rename wins. Returns ``source``
    unchanged when there is no user cache directory.
    """
    cache = _user_cache_dir()
    if cache is None:
        return source
    root = cache / "voiced"
    target = root / f"espeak-ng-data-{version}"
    if (target / "phontab").is_file():
        return str(target)
    try:
        root.mkdir(parents=True, exist_ok=True)
        _remove_stale_staging(root)
        staging = Path(tempfile.mkdtemp(prefix=".espeak-ng-data.", dir=root))
        try:
            copy = staging / "data"
            # cp -R copies a symlink as a symlink, so hand it the real directory.
            real = os.path.realpath(source)
            cloned = subprocess.run(["/bin/cp", "-cR", real, str(copy)], capture_output=True)
            if cloned.returncode != 0:
                shutil.rmtree(copy, ignore_errors=True)
                shutil.copytree(real, copy)
            if target.is_dir() and not (target / "phontab").is_file():
                # Left incomplete by a purge: move it aside so the rename can land.
                target.rename(staging / "incomplete")
            try:
                copy.rename(target)
            except OSError:
                # Another load put its copy in place first; use that one.
                if not (target / "phontab").is_file():
                    raise
        finally:
            shutil.rmtree(staging, ignore_errors=True)
    except OSError as exc:
        raise EspeakNotFound(f"could not copy espeak-ng data to {target}: {exc}") from exc
    return str(target)


def _remove_stale_staging(root: Path) -> None:
    """Best effort: remove staging directories a crashed load left behind."""
    cutoff = time.time() - _STALE_STAGING_SECONDS
    for entry in root.glob(".espeak-ng-data.*"):
        try:
            if entry.stat().st_mtime < cutoff:
                shutil.rmtree(entry, ignore_errors=True)
        except OSError:
            continue


def _espeak_data(data: str | None) -> str | None:
    """The data directory to load espeak-ng with.

    An explicit ``data`` is used as given. Without one the tokenizer would
    read the wheel's data from inside the virtual environment, whose path can
    be longer than the wheel's espeak-ng accepts, so a clone at a short path
    is used instead. The resolved path (``phonemizer`` resolves symlinks, so a
    short symlink does not help) must fit :data:`MAX_ESPEAK_DATA_PATH`, else
    this raises rather than let espeak-ng exit the process. The check applies
    to every data path: Homebrew's is about 45 bytes.
    """
    if data is None:
        bundled = _bundled_espeak()
        if bundled is None:
            return None
        data = _short_espeak_copy(*bundled)
    resolved = os.path.realpath(data)
    size = len(resolved.encode())
    if size > MAX_ESPEAK_DATA_PATH:
        raise EspeakNotFound(
            f"espeak-ng data path {resolved} is {size} bytes, longer than the "
            f"{MAX_ESPEAK_DATA_PATH} the espeak-ng from espeakng-loader accepts. Use "
            f"Homebrew's espeak-ng (`{ESPEAK_INSTALL_HINT}`), or set [tts] espeak_data "
            f"to a copy at a path of {MAX_ESPEAK_DATA_PATH} bytes or less."
        )
    return data


def _load_espeak() -> Any:
    from kokoro_onnx.config import EspeakConfig
    from kokoro_onnx.tokenizer import Tokenizer

    errors: list[str] = []
    for library, configured in espeak_candidates():
        # Raises on its own: a data path that is too long or cannot be copied
        # is not a missing espeak-ng.
        data = _espeak_data(configured)
        try:
            tokenizer = Tokenizer(EspeakConfig(lib_path=library, data_path=data))
            probe = tokenizer.phonemize("hello", "en-us")
        except (RuntimeError, OSError) as exc:  # how the tokenizer reports a bad library
            errors.append(f"{library}: {type(exc).__name__}: {exc}")
            continue
        if not probe:
            errors.append(f"{library}: phonemizer returned nothing (data path {data!r})")
            continue
        # kokoro_onnx's Tokenizer silently prefers PHONEMIZER_ESPEAK_LIBRARY
        # when that is set; log the library actually in use.
        in_use = os.environ.get("PHONEMIZER_ESPEAK_LIBRARY") or library
        log.info("kokoro: espeak-ng at %s, data %s", in_use, data)
        return tokenizer
    detail = "; ".join(errors) if errors else "no espeak-ng library found"
    raise EspeakNotFound(
        "Kokoro needs espeak-ng for pronunciation but it is not installed or not "
        f"working ({detail}). Install it with `{ESPEAK_INSTALL_HINT}`, or point "
        "[tts] espeak_library and espeak_data at a working copy."
    )


def _phonemize(tokenizer: Any, text: str, voice: str) -> str:
    return str(tokenizer.phonemize(text, KOKORO_LANGUAGES[voice[0]].espeak))


def _synth(model: Any, tokenizer: Any, pack: Any, sentence: str, voice: str, speed: float) -> bytes:
    phonemes = _phonemize(tokenizer, sentence, voice)
    if not phonemes:
        return b""
    parts: list[NDArray[np.float32]] = []
    # Sentences are at most 300 characters and phoneme strings run up to about
    # 1.2 times the text (more for acronym-heavy text), so a sentence can
    # exceed the model's 510-phoneme context; cut at spaces when it does.
    for chunk in cut_to_length(phonemes, MAX_PHONEMES):
        out = model(chunk, pack[len(chunk) - 1], speed, return_output=True)
        parts.append(np.array(out.audio, copy=False).reshape(-1))
    return float32_to_pcm16(np.concatenate(parts))
