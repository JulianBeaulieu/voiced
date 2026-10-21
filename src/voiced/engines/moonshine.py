"""Moonshine v2 Streaming on the CPU through ``moonshine-voice``. Selectable,
not the default.

Moonshine's streaming models (Tiny 34M, Small 123M, Medium 245M) encode audio
as it arrives and decode the open line every half second, in ONNX Runtime on
the CPU (``moonshine-voice`` bundles it; Core ML is off upstream). This engine
(see ``docs/decisions/0026-stt-moonshine.md``):

* streams through the runtime's own :class:`Stream`: ``add_audio`` passes the
  samples to the stream on the engine's :class:`ModelThread` and waits for
  it; the stream decodes every :data:`UPDATE_S` of audio on its own, and
  ``partial`` is the text of its lines so far;
* finalizes on voiced's end signal, not Moonshine's: ``Stream.stop()`` runs a
  last pass over the audio since the previous one and completes the open
  line. Moonshine's speech detector only splits the session into lines,
  which become the transcript's segments, with the lines' times;
* runs on the CPU only, so it never waits for or holds the GPU (ADR 0019);
* loads its model from voiced's pinned snapshot of the publisher's asset
  repository on the Hub (``voiced.models``; each catalogued model is one
  folder of it), so the runtime never reaches its CDN.

Word timestamps are off: streamed lines report broken word times (every word
after the first few at the line's start), and batch word times need a second
decoder (82 MB for Small) and cost about 20 ms per finalize (ADR 0026).

Everything that imports ``moonshine_voice`` runs inside ``load()``, so this
module imports (the entry-point registry does so) without the ``moonshine``
extra, which macOS 14 does not install.
"""

from __future__ import annotations

import logging
import platform
import posixpath
import time
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray

from voiced.audio import check_samples, clip_segment, stt_input
from voiced.engines.base import (
    STT_SAMPLE_RATE,
    AudioInput,
    EngineError,
    EngineNotLoaded,
    Segment,
    STTEngine,
    STTStreamSession,
    Transcript,
    UnsupportedLanguage,
)
from voiced.engines.registry import ModelRefused
from voiced.models import default_model, engine, find
from voiced.text import collapse_whitespace, primary_subtag
from voiced.threads import ModelThread

__all__ = ["MODELS", "MoonshineEngine", "MoonshineStreamSession"]

log = logging.getLogger(__name__)

_MOONSHINE = engine("moonshine")
if _MOONSHINE is None:  # pragma: no cover - the catalogue ships with the engine
    raise ImportError("voiced.models.CATALOGUE has no moonshine engine")
MODELS = tuple(m.id for m in _MOONSHINE.models)
"""The English streaming models in the catalogue, by their model card ids."""

ARCH = "SMALL_STREAMING"
"""The ``moonshine_voice.ModelArch`` every model loads as: a streaming model
takes its geometry from its ``streaming_config.json``, so any streaming arch
loads any streaming folder, with the same transcripts (ADR 0026)."""

UPDATE_S = 0.5
"""The stream decodes the open line every this much audio: Moonshine's
default, which keeps the audio left for the last pass under half a second."""
WARMUP_S = 1.0
MIN_MACOS_DARWIN = 24
"""``moonshine-voice`` has wheels for macOS 15 (Darwin 24) and later only."""

_SR = STT_SAMPLE_RATE


def model_folder(model_id: str) -> str:
    """The folder of the catalogued ``model_id`` in its repository: the one
    its patterns name."""
    found = find(model_id)
    if found is None:
        raise KeyError(model_id)
    return posixpath.dirname(found[1].patterns[0])


class MoonshineEngine(STTEngine):
    name = "moonshine"
    model_id = default_model("moonshine").id
    languages = ["en"]
    license = default_model("moonshine").license
    max_streams = 1
    """One session at a time: every pass runs on the engine's one CPU thread,
    and a second stream's pass would delay the first one's finalize."""

    def __init__(self, model_id: str | None = None) -> None:
        super().__init__(model_id)
        if self.model_id not in MODELS and not Path(self.model_id).expanduser().is_dir():
            raise ModelRefused(
                f"{self.model_id} is not in voiced's model catalogue; its Moonshine models "
                f"are {', '.join(MODELS)}, or use a local model folder"
            )
        self._transcriber: Any = None
        self._thread = ModelThread("moonshine")

    @classmethod
    def unavailable_reason(cls) -> str | None:
        """The engine catalogue's hook: no ``moonshine-voice`` below macOS 15."""
        darwin = platform.release().split(".")[0]
        if platform.system() == "Darwin" and darwin.isdigit() and int(darwin) < MIN_MACOS_DARWIN:
            return "needs macOS 15 or later"
        return None

    @classmethod
    def normalize_language(cls, language: str | None) -> str:
        """``"en-US"`` to ``"en"``; raise :class:`UnsupportedLanguage` otherwise."""
        if language is None:
            return "en"
        code = primary_subtag(language.strip())
        if code not in cls.languages:
            raise UnsupportedLanguage(language)
        return code

    async def load(self) -> None:
        await self._thread.run(self._load_blocking)

    def _load_blocking(self) -> None:
        """Build in locals and assign ``_transcriber`` last, so a failure
        part-way leaves the engine unloaded."""
        try:
            from moonshine_voice import ModelArch, Transcriber
        except ImportError as exc:
            raise RuntimeError(_missing_runtime()) from exc

        from voiced.models import snapshot

        started = time.perf_counter()
        root = snapshot(self.model_id, check_only=True)
        if self.model_id in MODELS:
            root = root / model_folder(self.model_id)
        arch = ModelArch[ARCH]
        transcriber = Transcriber(str(root), arch, update_interval=UPDATE_S)
        # Both paths once, so a broken model fails here and the first request
        # does not pay for ONNX Runtime's first runs.
        silence = np.zeros(round(WARMUP_S * _SR), dtype=np.float32)
        transcriber.transcribe_without_streaming(silence.tolist(), _SR)
        stream = transcriber.create_stream(update_interval=UPDATE_S)
        try:
            stream.start()
            stream.add_audio(silence.tolist(), _SR)
            _stop(stream)
        finally:
            stream.close()
        self._transcriber = transcriber
        log.info(
            "moonshine: loaded %s (%s) in %.1fs",
            self.model_id,
            arch.name,
            time.perf_counter() - started,
        )

    def _require_loaded(self) -> Any:
        if self._transcriber is None:
            raise EngineNotLoaded(self.name)
        return self._transcriber

    async def transcribe(self, audio: AudioInput, language: str | None = None) -> Transcript:
        transcriber = self._require_loaded()
        lang = self.normalize_language(language)
        samples = stt_input(audio)
        if samples is None:
            return Transcript(text="", language=lang, duration_s=audio.duration_s)
        result = await self._thread.run(_transcribe, transcriber, samples)
        return _to_transcript(result.lines, lang, audio.duration_s)

    async def stream(self, language: str | None = None) -> STTStreamSession:
        transcriber = self._require_loaded()
        lang = self.normalize_language(language)
        return MoonshineStreamSession(self._thread, transcriber, lang)


class MoonshineStreamSession(STTStreamSession):
    """One Moonshine :class:`Stream`, fed on the model thread.

    The stream is created and started on the first ``add_audio``, so opening
    a session costs nothing. Each ``add_audio`` waits for the stream to take
    the chunk, a pass included when one is due: the backpressure.
    ``finalize()`` stops the stream (its last pass) and returns every line.
    ``close()`` frees it.
    """

    def __init__(self, thread: ModelThread, transcriber: Any, language: str) -> None:
        self._thread = thread
        self._transcriber = transcriber
        self._language = language
        self._stream: Any = None
        self._lines: dict[int, str] = {}  # line id to text, in order; written on the thread
        self._failure: BaseException | None = None
        self._added = 0
        self._closed = False

    async def add_audio(self, samples: NDArray[np.float32]) -> None:
        self._check_open()
        arr = check_samples(samples)
        if len(arr) == 0:
            return
        self._added += len(arr)
        await self._thread.run(self._add_blocking, arr)
        self._check_open()

    def _add_blocking(self, chunk: NDArray[np.float32]) -> None:
        """On the model thread: start the stream if new, then add ``chunk``;
        the stream runs a pass when :data:`UPDATE_S` of audio is due."""
        if self._stream is None:
            stream = self._transcriber.create_stream(update_interval=UPDATE_S)
            stream.add_listener(self._on_event)
            self._stream = stream
            stream.start()
        self._stream.add_audio(chunk.tolist(), _SR)

    def _on_event(self, event: Any) -> None:
        """The stream's listener, on the model thread. ``event`` is the
        library's ``Error`` (``.error``) or one of its line events
        (``LineStarted``, ``LineUpdated``, ``LineTextChanged``,
        ``LineCompleted``: ``.line``)."""
        error = getattr(event, "error", None)
        if isinstance(error, BaseException):
            self._failure = error
            return
        line = getattr(event, "line", None)
        if line is not None:
            self._lines[line.line_id] = line.text

    def partial(self) -> str:
        # A copy: the listener writes _lines on the model thread meanwhile.
        return collapse_whitespace(" ".join(list(self._lines.values())))

    async def finalize(self) -> Transcript:
        self._check_open()
        self._closed = True
        duration = self._added / _SR
        if self._stream is None:
            return Transcript(text="", language=self._language, duration_s=duration)
        try:
            result = await self._thread.run(_stop, self._stream)
        except RuntimeError:
            if self._failure is not None:  # what the listener was told
                raise self._failure from None
            raise
        return _to_transcript(result.lines, self._language, duration)

    async def close(self) -> None:
        self._closed = True
        stream, self._stream = self._stream, None
        if stream is not None:
            await self._thread.run(stream.close)

    def _check_open(self) -> None:
        if self._failure is not None:
            raise self._failure
        if self._closed:
            raise EngineError("stream session is closed")


# --- helpers ------------------------------------------------------------------


def _missing_runtime() -> str:
    reason = MoonshineEngine.unavailable_reason()
    if reason is not None:
        return f"the moonshine engine {reason} (moonshine-voice has no older wheel)"
    return (
        "the moonshine engine needs the moonshine extra, which is not installed "
        "(from a checkout: uv sync --extra moonshine)"
    )


def _transcribe(transcriber: Any, samples: NDArray[np.float32]) -> Any:
    """On the model thread: the list conversion of a long upload too."""
    return transcriber.transcribe_without_streaming(samples.tolist(), _SR)


def _stop(stream: Any) -> Any:
    """``stream.stop()``: its last pass and every line. The library reports a
    failed pass only to the stream's listeners and returns ``None``."""
    result = stream.stop()
    if result is None:
        raise RuntimeError("moonshine: the last pass returned no transcript")
    return result


def _to_transcript(lines: Any, language: str, duration_s: float) -> Transcript:
    """One segment per Moonshine line with text, its times clipped to the input."""
    segments: list[Segment] = []
    for line in lines:
        text = collapse_whitespace(line.text)
        if text:
            start = float(line.start_time)
            segments.append(clip_segment(start, start + float(line.duration), text, duration_s))
    return Transcript(
        text=" ".join(s.text for s in segments),
        language=language,
        duration_s=duration_s,
        segments=tuple(segments),
    )
