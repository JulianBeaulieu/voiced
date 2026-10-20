"""Kokoro-82M on MLX, with espeak-ng phonemes.

Pronunciation path (see ``docs/decisions/0009-tts-kokoro-espeak-tokens.md``):
the already normalized text is split into sentences, each sentence is
phonemized with espeak-ng through ``kokoro_onnx.tokenizer.Tokenizer``, and
the phoneme string is fed straight to the MLX model together with the
voice's style vector. mlx-audio's own pipeline (misaki G2P) is bypassed.

espeak-ng is an external, GPL-3 program and is never vendored. The engine
uses the library and data directory named by ``VOICED_ESPEAK_LIBRARY`` and
``VOICED_ESPEAK_DATA`` when those are set (the services set them from
``[tts] espeak_library`` and ``espeak_data``), else the copy in the
``espeakng-loader`` wheel that ``kokoro-onnx`` installs, else the Homebrew
installation (``brew install espeak-ng``), and fails at ``load()`` with the
remedies if none works (ADR 0018, :func:`espeak_candidates`). The wheel's
data, which is also what an override without a data directory reads, is
loaded from a clone at a short path (``_short_espeak_copy``).

Each residual block of a sentence's graph is submitted to the GPU as soon as
it is built, after waiting for any speech-to-text encoder call that holds the
GPU (``voiced.gpu``, ADR 0019): the STT service's calls go first.

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
from collections import deque
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import numpy as np
from numpy.typing import NDArray

from voiced import gpu
from voiced.audio import float32_to_pcm16
from voiced.config import ENV_ESPEAK_DATA, ENV_ESPEAK_LIBRARY
from voiced.engines.base import TTS_SAMPLE_RATE, Voice
from voiced.engines.sentence import WARMUP_TEXT, SentenceTTSEngine
from voiced.text.sentences import cut_to_length

log = logging.getLogger(__name__)

__all__ = [
    "ENV_ESPEAK_DATA",
    "ENV_ESPEAK_LIBRARY",
    "ESPEAK_COPIES",
    "ESPEAK_INSTALL_HINT",
    "ESPEAK_REMEDY",
    "EspeakCandidate",
    "EspeakNotFound",
    "KokoroEngine",
    "KokoroLanguage",
    "espeak_candidates",
    "espeak_data_path",
    "kokoro_voices",
]

ESPEAK_INSTALL_HINT = "brew install espeak-ng"
ESPEAK_COPIES = "the espeak-ng in voiced's environment (espeakng-loader) nor Homebrew's"
"""Follows "neither": the two copies looked for when there is no override."""
ESPEAK_REMEDY = (
    "Reinstall voiced (Repair in the menu bar app), or install Homebrew's "
    f"espeak-ng (`{ESPEAK_INSTALL_HINT}`), or point [tts] espeak_library and "
    "espeak_data at a working copy."
)
HOMEBREW_PREFIXES = ("/opt/homebrew/opt/espeak-ng", "/usr/local/opt/espeak-ng")
MAX_ESPEAK_DATA_PATH = 159
"""Bytes. The espeak-ng in the ``espeakng-loader`` wheel keeps its data path in
a 160-byte buffer; a longer path makes it print ``Error processing file`` and
call ``exit(1)``, which takes the whole process down. Homebrew's build has no
such limit."""
ESPEAK_DATA_FILES = ("phontab", "phondata", "phonindex", "intonations", "en_dict")
"""What a data directory needs for English. espeak-ng exits the process when
one is missing, so a clone without all of them is made again."""
_CS_DARWIN_USER_CACHE_DIR = 65538
"""``confstr`` name for the per-user cache directory under ``/var/folders``."""
_STALE_STAGING_SECONDS = 3600
MAX_PHONEMES = 510
"""Kokoro's context is 512 tokens including the two padding tokens."""
YIELD_POINT_BLOCKS = ("AdainResBlk1d", "AdaINResBlock1")
"""Classes in ``mlx_audio.tts.models.kokoro.istftnet`` (called as
``block(x, s)``) after which a sentence's graph is submitted, about 20 per
sentence: the decoder's and the prosody predictor's residual blocks and the
vocoder's. A shorter interval costs Kokoro more, a longer one makes the STT
wait longer (ADR 0019)."""
BLOCKS_IN_FLIGHT = 2
"""Blocks a sentence may have submitted and not finished. Waiting for each
block before the next (1) left the GPU idle while MLX encoded the next one:
first audio 30 ms later. Unbounded, MLX let ten command buffers run ahead,
all queued in front of an encoder call that started (ADR 0019)."""


class _Submissions:
    """One sentence's submissions: its yielder and the blocks submitted and
    not yet waited for, oldest first."""

    def __init__(self) -> None:
        self.yielder = gpu.Yielder()
        self.in_flight: deque[Any] = deque()


_submissions: ContextVar[_Submissions | None] = ContextVar(
    "voiced_kokoro_submissions", default=None
)


class EspeakNotFound(RuntimeError):
    """espeak-ng could not be found or does not work. Not a client error."""


@dataclass(frozen=True)
class EspeakCandidate:
    """An espeak-ng copy to try: :func:`espeak_candidates` is the one place
    that finds them and decides their order."""

    library: str
    data: str | None
    """The data directory as found: configured, the keg's, or the wheel's
    inside the environment. ``None`` leaves it to the library."""
    source: Literal["override", "wheel", "homebrew"]
    wheel_version: str | None = None
    """Set when ``data`` is the wheel's: it is loaded from a short clone named
    after this version (:func:`espeak_data_path`)."""


@dataclass(frozen=True)
class _Wheel:
    library: str
    data: str
    version: str


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


def espeak_candidates() -> list[EspeakCandidate]:
    """The espeak-ng copies to try, most preferred first.

    ``VOICED_ESPEAK_LIBRARY`` (with optional ``VOICED_ESPEAK_DATA``), which
    ``voiced`` sets from ``[tts] espeak_library`` and ``espeak_data``, is the one
    override and is the only candidate when set; a path that does not exist
    is an error naming it. Without ``espeak_data`` it reads the wheel's data.
    Otherwise the library in the ``espeakng-loader`` wheel, so every install
    runs the version in the lock, then the Homebrew keg for Apple Silicon and
    for Intel, whichever exists. Nothing is copied or loaded here.
    """
    wheel = _bundled_espeak()
    override = os.environ.get(ENV_ESPEAK_LIBRARY)
    if override:
        data = os.environ.get(ENV_ESPEAK_DATA) or None
        if not Path(override).is_file():
            raise EspeakNotFound(f"[tts] espeak_library {override} is not a file")
        if data and not Path(data).is_dir():
            raise EspeakNotFound(f"[tts] espeak_data {data} is not a directory")
        if data is None and wheel is not None:
            return [EspeakCandidate(override, wheel.data, "override", wheel.version)]
        return [EspeakCandidate(override, data, "override")]
    found = []
    if wheel is not None and Path(wheel.library).is_file():
        found.append(EspeakCandidate(wheel.library, wheel.data, "wheel", wheel.version))
    return found + [
        EspeakCandidate(
            f"{prefix}/lib/libespeak-ng.dylib", f"{prefix}/share/espeak-ng-data", "homebrew"
        )
        for prefix in HOMEBREW_PREFIXES
        if Path(f"{prefix}/lib/libespeak-ng.dylib").is_file()
    ]


def espeak_data_path(candidate: EspeakCandidate) -> str | None:
    """The data directory loading ``candidate`` would use, without copying
    anything: the short clone's path for the wheel's data (its source when
    there is no user cache directory).

    The resolved path (``phonemizer`` resolves symlinks, so a short symlink
    does not help) must fit :data:`MAX_ESPEAK_DATA_PATH`, else this raises
    rather than let espeak-ng exit the process; so does a clone that is not
    there yet and cannot be made. The check applies to every data path:
    Homebrew's is about 45 bytes. Load and ``voiced doctor`` both use it.
    """
    if candidate.data is None:
        return None
    path = candidate.data
    if candidate.wheel_version is not None:
        target = _clone_target(candidate.wheel_version)
        if target is not None:
            if not _complete(target) and not _can_create(target):
                raise EspeakNotFound(f"cannot copy espeak-ng data to {target}: not writable")
            path = str(target)
    resolved = os.path.realpath(path)
    size = len(resolved.encode())
    if size > MAX_ESPEAK_DATA_PATH:
        raise EspeakNotFound(
            f"espeak-ng data path {resolved} is {size} bytes, longer than the "
            f"{MAX_ESPEAK_DATA_PATH} the espeak-ng from espeakng-loader accepts"
        )
    return path


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

    @classmethod
    def download_patterns(cls) -> list[str]:
        """The config and the weights, with the voice packs in ``voices/``
        (``voiced.models``); not the sample WAVs or the ``.pt`` copies."""
        return ["config.json", "*.safetensors"]

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

        from voiced.models import pinned_revision, snapshot

        patterns = self.download_patterns()
        # Both at the pinned revision, from the cache voiced install filled.
        # load_model takes the repo id: mlx-audio tells a Kokoro model by
        # its name, which a snapshot directory does not always carry. Its
        # resolve of the same revision and patterns is a cache hit.
        root = snapshot(self.model_id, patterns)
        model = load_model(
            self.model_id, revision=pinned_revision(self.model_id), allow_patterns=patterns
        )
        _install_yield_points()
        rate = int(model.sample_rate)
        if rate != TTS_SAMPLE_RATE:
            raise RuntimeError(f"Kokoro model reports {rate} Hz, expected {TTS_SAMPLE_RATE}")
        all_voices = kokoro_voices(self.languages)
        packs = self._load_voice_packs(root, all_voices)
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

    def _load_voice_packs(self, root: Path, voices: list[Voice]) -> dict[str, Any]:
        from mlx_audio.tts.models.kokoro.voice import load_voice_tensor

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


def _bundled_espeak() -> _Wheel | None:
    """The espeak-ng in the ``espeakng-loader`` wheel, or ``None`` when it is
    not installed or does not say where its files are."""
    try:
        import espeakng_loader

        library = espeakng_loader.get_library_path()
        data = str(espeakng_loader.get_data_path())
    except (ImportError, AttributeError, RuntimeError):
        return None
    try:
        version = importlib.metadata.version("espeakng-loader")
    except importlib.metadata.PackageNotFoundError:
        version = "unknown"
    return _Wheel(library, data, version)


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
    every one of :data:`ESPEAK_DATA_FILES`) is reused and never replaced, so
    a running service never sees its data change under it; a wheel upgrade
    gets a new directory, and one the system purged, even in part, is made
    again. Concurrent loads each clone into their own staging directory and
    the first rename wins. Returns ``source`` unchanged when there is no user
    cache directory.
    """
    target = _clone_target(version)
    if target is None:
        return source
    root = target.parent
    if _complete(target):
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
            if target.is_dir() and not _complete(target):
                # Left incomplete by a purge: move it aside so the rename can land.
                target.rename(staging / "incomplete")
            try:
                copy.rename(target)
            except OSError:
                # Another load put its copy in place first; use that one.
                if not _complete(target):
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


def _clone_target(version: str) -> Path | None:
    """Where the wheel's data of ``version`` is cloned, or ``None`` when the
    system has no user cache directory."""
    cache = _user_cache_dir()
    return None if cache is None else cache / "voiced" / f"espeak-ng-data-{version}"


def _complete(data: Path) -> bool:
    return all((data / name).is_file() for name in ESPEAK_DATA_FILES)


def _can_create(path: Path) -> bool:
    """Whether the nearest existing directory above ``path`` is writable."""
    parent = path.parent
    while not parent.exists() and parent != parent.parent:
        parent = parent.parent
    return parent.is_dir() and os.access(parent, os.W_OK | os.X_OK)


def _espeak_data(candidate: EspeakCandidate) -> str | None:
    """:func:`espeak_data_path` for ``candidate``, with the wheel's data
    cloned there first."""
    path = espeak_data_path(candidate)
    if candidate.wheel_version is not None and candidate.data is not None:
        _short_espeak_copy(candidate.data, candidate.wheel_version)
    return path


def _try_espeak(candidate: EspeakCandidate) -> Any:
    """A tokenizer that phonemizes with ``candidate``, or :class:`EspeakNotFound`
    saying why not."""
    from kokoro_onnx.config import EspeakConfig
    from kokoro_onnx.tokenizer import Tokenizer

    data = _espeak_data(candidate)
    try:
        tokenizer = Tokenizer(EspeakConfig(lib_path=candidate.library, data_path=data))
        probe = tokenizer.phonemize("hello", "en-us")
    except (RuntimeError, OSError) as exc:  # how the tokenizer reports a bad library
        raise EspeakNotFound(f"{type(exc).__name__}: {exc}") from exc
    if not probe:
        raise EspeakNotFound(f"phonemizer returned nothing (data path {data!r})")
    # kokoro_onnx's Tokenizer silently prefers PHONEMIZER_ESPEAK_LIBRARY
    # when that is set; log the library actually in use.
    in_use = os.environ.get("PHONEMIZER_ESPEAK_LIBRARY") or candidate.library
    log.info("kokoro: espeak-ng at %s, data %s", in_use, data)
    return tokenizer


def _load_espeak() -> Any:
    errors: list[str] = []
    for candidate in espeak_candidates():
        try:
            return _try_espeak(candidate)
        except EspeakNotFound as exc:
            errors.append(f"{candidate.library}: {exc}")
    problem = (
        f"no copy it tried works ({'; '.join(errors)})"
        if errors
        else f"it found neither {ESPEAK_COPIES}"
    )
    raise EspeakNotFound(f"Kokoro needs espeak-ng for pronunciation but {problem}. {ESPEAK_REMEDY}")


def _install_yield_points() -> None:
    """Make each of :data:`YIELD_POINT_BLOCKS` a submission point: wait
    until fewer than :data:`BLOCKS_IN_FLIGHT` of the sentence's blocks are
    unfinished, then for the sentence's :class:`voiced.gpu.Yielder`, then
    submit the block's output (:func:`_submit`).

    Without this a sentence is one ``mx.eval`` of its whole graph, its work
    is queued on the GPU at once, and an STT encoder call waits behind it.
    ``async_eval`` does not wait for the GPU, so the next blocks are built
    and encoded while earlier ones run. It must not run inside an
    ``mx.compile`` trace; ``_synth`` never passes the model a ``decoder``
    to compile.

    Patches the classes once per process; the pinned mlx-audio range
    (pyproject) is what it was checked against. It fails open: a class that
    is missing is logged and skipped, and a block whose output is not an
    array is returned untouched, so a changed mlx-audio loses the priority,
    not the service.
    """
    import mlx.core as mx
    from mlx_audio.tts.models.kokoro import istftnet

    for name in YIELD_POINT_BLOCKS:
        cls = getattr(istftnet, name, None)
        if cls is None:
            log.warning("kokoro: mlx-audio has no %s; GPU priority for STT is partial", name)
            continue
        if getattr(cls, "_voiced_yield_point", False):
            continue
        call = cls.__call__

        def submit(self: Any, *args: Any, _call: Any = call, **kwargs: Any) -> Any:
            out = _call(self, *args, **kwargs)
            current = _submissions.get()
            if current is None or not isinstance(out, mx.array):  # not inside _synth
                return out
            while len(current.in_flight) >= BLOCKS_IN_FLIGHT:
                mx.eval(current.in_flight.popleft())
            current.yielder()
            _submit(out)
            current.in_flight.append(out)
            return out

        cls.__call__ = submit
        cls._voiced_yield_point = True


def _submit(out: Any) -> None:
    """Queue ``out`` on the GPU without waiting for it."""
    import mlx.core as mx

    async_eval: Any = mx.async_eval  # untyped in mlx's stubs
    async_eval(out)


def _phonemize(tokenizer: Any, text: str, voice: str) -> str:
    return str(tokenizer.phonemize(text, KOKORO_LANGUAGES[voice[0]].espeak))


def _synth(model: Any, tokenizer: Any, pack: Any, sentence: str, voice: str, speed: float) -> bytes:
    phonemes = _phonemize(tokenizer, sentence, voice)
    if not phonemes:
        return b""
    parts: list[NDArray[np.float32]] = []
    submissions = _Submissions()
    token = _submissions.set(submissions)
    try:
        # Sentences are at most 300 characters and phoneme strings run up to
        # about 1.2 times the text (more for acronym-heavy text), so a sentence
        # can exceed the model's 510-phoneme context; cut at spaces when it does.
        for chunk in cut_to_length(phonemes, MAX_PHONEMES):
            submissions.yielder()
            out = model(chunk, pack[len(chunk) - 1], speed, return_output=True)
            parts.append(np.array(out.audio, copy=False).reshape(-1))
    finally:
        _submissions.reset(token)
    return float32_to_pcm16(np.concatenate(parts))
