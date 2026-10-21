"""Whisper engine tests: first with stand-in ``mlx`` and ``mlx_audio`` modules
and the real Silero gate (they run on CI), then against the real models
(``model``): ``VOICED_MODEL_TESTS=1 uv run pytest tests/stt/test_whisper.py -m model -s``."""

from __future__ import annotations

import asyncio
import statistics
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest
from httpx import AsyncClient
from numpy.typing import NDArray

from tests.stt.conftest import (
    TurnRecorder,
    blocking_first_call,
    load_fixture,
    stub_mlx_audio,
    until,
    word_accuracy,
)
from voiced import models
from voiced.engines import whisper
from voiced.engines.base import (
    AudioInput,
    EngineError,
    EngineNotLoaded,
    Segment,
    UnsupportedLanguage,
)
from voiced.engines.catalogue import engines_list
from voiced.engines.registry import ModelRefused
from voiced.engines.whisper import LANGUAGES, WhisperEngine

SR = 16_000
SPEECH = load_fixture("speech_en.wav")
"""Real speech, so the real Silero gate lets it through."""
HUM = (0.03 * np.sin(2 * np.pi * 50 * np.arange(3 * SR) / SR)).astype(np.float32)
"""Mains hum at -30 dBFS: above the silence floor, and Whisper writes
"Thank you." for it."""
CATALOGUED = [m.id for m in models.engine("whisper").models]  # type: ignore[union-attr]


def test_registered_with_its_languages_and_one_stream() -> None:
    engine = WhisperEngine()
    assert engine.languages[0] == "en" and len(engine.languages) == 100
    assert len(set(engine.languages)) == 100
    assert engine.max_streams == 1
    assert engine.license == "MIT"
    stt = {e["name"]: e for e in engines_list()["stt"]}
    assert stt["whisper"]["models"] == CATALOGUED


def test_its_models_are_catalogued_8bit_first() -> None:
    assert CATALOGUED == [
        "mlx-community/whisper-large-v3-turbo-asr-8bit",
        "mlx-community/whisper-large-v3-turbo-asr-fp16",
    ]
    assert WhisperEngine.model_id == CATALOGUED[0]


def test_a_turbo_checkpoint_outside_the_catalogue_is_refused(tmp_path: Path) -> None:
    """mlx-whisper's layout, which mlx-audio cannot load, among them."""
    for repo in ("mlx-community/whisper-large-v3-turbo", "mlx-community/whisper-large-v3-turbo-q4"):
        with pytest.raises(ModelRefused, match=r"are mlx-community/whisper-large-v3-turbo-asr"):
            WhisperEngine(repo)
    assert WhisperEngine(str(tmp_path)).model_id == str(tmp_path)  # a local model is not


def test_the_module_imports_without_mlx_audio() -> None:
    """The registry imports every engine module; mlx-audio is imported in load()."""
    code = "import sys; sys.modules['mlx_audio'] = None; import voiced.engines.whisper"
    subprocess.run([sys.executable, "-c", code], check=True)


@pytest.mark.parametrize(
    "hint, code",
    [
        (None, None),
        ("EN-us", "en"),
        ("pt_BR", "pt"),
        (" de ", "de"),
        ("yue-HK", "yue"),
        ("jv", "jv"),
    ],
)
def test_language_hints_are_bcp47(hint: str | None, code: str | None) -> None:
    assert WhisperEngine.normalize_language(hint) == code


@pytest.mark.parametrize("hint", ["xx", "jw", "fil", ""])
def test_a_language_the_model_does_not_know_is_refused(hint: str) -> None:
    """``jw`` is Whisper's own code for Javanese; voiced takes BCP-47 ``jv``."""
    with pytest.raises(UnsupportedLanguage):
        WhisperEngine.normalize_language(hint)


# --- stand-ins ------------------------------------------------------------


class _Model:
    """Stands in for mlx-audio's Whisper model: one segment per 2 s of input,
    in the language given, else in ``detects``."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.locked: list[bool] = []
        self.turns = TurnRecorder()  # the fixture's, once installed
        self.texts = [" Turn on the kitchen lights", "  and the heating. "]
        self.detects = "en"

    def generate(self, samples: NDArray[np.float32], **kwargs: Any) -> Any:
        self.calls.append({"samples": len(samples), **kwargs})
        self.locked.append(self.turns.held)
        end = len(samples) / SR
        starts = np.arange(0.0, end, 2.0)
        segments = [
            {"start": float(s), "end": float(min(s + 2.0, end) + 0.5), "text": self.texts[i % 2]}
            for i, s in enumerate(starts)
        ]
        language = kwargs["language"] or self.detects
        return SimpleNamespace(
            text="".join(s["text"] for s in segments), segments=segments, language=language
        )


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[_Model, list[str]]:
    """``mlx.core``, ``mlx_audio``'s loader and the snapshot."""
    model, loaded = _Model(), []

    def load_model(path: Path) -> Any:
        loaded.append(str(path.relative_to(tmp_path)))
        return model

    monkeypatch.setattr(models, "snapshot", lambda repo, check_only: tmp_path / repo)
    model.turns = stub_mlx_audio(monkeypatch, whisper, load_model)
    return model, loaded


async def _loaded(fake: tuple[_Model, list[str]]) -> WhisperEngine:
    engine = WhisperEngine()
    await engine.load()
    fake[0].calls.clear()
    fake[0].locked.clear()
    return engine


# --- with the stand-ins -----------------------------------------------------


async def test_load_reads_the_pinned_snapshot_and_warms_up(fake: tuple[_Model, list[str]]) -> None:
    model, loaded = fake
    await WhisperEngine().load()
    assert loaded == [WhisperEngine.model_id]
    assert [(c["language"], c["word_timestamps"]) for c in model.calls] == [("en", True)]


async def test_a_load_that_fails_leaves_it_unloaded(
    fake: tuple[_Model, list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("no Metal")

    monkeypatch.setattr(fake[0], "generate", broken)
    engine = WhisperEngine()
    with pytest.raises(RuntimeError, match="no Metal"):
        await engine.load()
    with pytest.raises(EngineNotLoaded):
        await engine.transcribe(AudioInput(SPEECH))


def _gaps(*parts: NDArray[np.float32] | float) -> NDArray[np.float32]:
    """Audio from speech and seconds of digital silence, in order."""
    return np.concatenate(
        [np.zeros(round(p * SR), np.float32) if isinstance(p, float) else p for p in parts]
    )


async def test_batch_has_word_timed_segments_in_input_time(
    fake: tuple[_Model, list[str]],
) -> None:
    """One span of speech, with silence around it that never reaches the
    model; segment times count from the input's start."""
    model, _ = fake
    engine = await _loaded(fake)
    samples = _gaps(5.0, SPEECH, 1.0)
    transcript = await engine.transcribe(AudioInput(samples), "en-GB")
    assert transcript.text == "Turn on the kitchen lights and the heating."
    assert transcript.language == "en"
    (call,) = model.calls
    assert call["language"] == "en" and call["word_timestamps"] is True
    assert len(SPEECH) < call["samples"] < len(SPEECH) + SR
    first, second = transcript.segments  # whitespace collapsed
    assert 4.5 < first.start < 5.0 and first.text == "Turn on the kitchen lights"
    assert second == Segment(round(first.start + 2.0, 3), second.end, "and the heating.")
    assert model.locked == [False]  # batch does not hold the GPU lock


async def test_long_silences_split_the_input_and_the_first_span_names_the_language(
    fake: tuple[_Model, list[str]],
) -> None:
    """Over 30 s with two silences of 40 and 25 s: two decodes, neither
    seeing the silence, times kept, and the language detected on the first
    span passed to the second."""
    model, _ = fake
    model.detects = "es"
    engine = await _loaded(fake)
    samples = _gaps(SPEECH, 40.0, SPEECH, 25.0)
    transcript = await engine.transcribe(AudioInput(samples))
    assert [c["language"] for c in model.calls] == [None, "es"]
    assert all(c["samples"] < len(SPEECH) + SR for c in model.calls)
    assert transcript.language == "es"
    starts = [s.start for s in transcript.segments]
    assert starts[0] < 0.5 and 43.0 < starts[2] < 44.0, starts
    assert transcript.text.count("Turn on the kitchen lights") == 2


async def test_speech_after_30_s_of_silence_is_found_and_its_language_detected(
    fake: tuple[_Model, list[str]],
) -> None:
    fake[0].detects = "es"
    engine = await _loaded(fake)
    transcript = await engine.transcribe(AudioInput(_gaps(35.0, SPEECH)))
    (call,) = fake[0].calls
    assert call["language"] is None and call["samples"] < len(SPEECH) + SR
    assert transcript.language == "es" and transcript.segments[0].start > 34.5


@pytest.mark.parametrize("detected, code", [("de", "de"), ("jw", "jv"), ("yue", "yue")])
async def test_the_detected_language_comes_back_as_bcp47(
    fake: tuple[_Model, list[str]], detected: str, code: str
) -> None:
    fake[0].detects = detected
    engine = await _loaded(fake)
    transcript = await engine.transcribe(AudioInput(SPEECH))
    assert fake[0].calls[0]["language"] is None
    assert transcript.language == code


async def test_javanese_is_passed_by_whispers_own_code(fake: tuple[_Model, list[str]]) -> None:
    engine = await _loaded(fake)
    transcript = await engine.transcribe(AudioInput(SPEECH), "jv")
    assert fake[0].calls[0]["language"] == "jw"
    assert transcript.language == "jv"


@pytest.mark.parametrize(
    "samples",
    [np.zeros(3 * SR, dtype=np.float32), HUM, SPEECH[: whisper.MIN_SAMPLES - 1]],
    ids=["silence", "hum", "too-short"],
)
async def test_input_without_speech_never_reaches_the_model(
    fake: tuple[_Model, list[str]], samples: NDArray[np.float32]
) -> None:
    """Whisper writes "Thank you." for each of these. Silence and the slice
    under 100 ms stop at ``stt_input``; the hum, above the silence floor,
    stops at Silero."""
    engine = await _loaded(fake)
    for transcript in (
        await engine.transcribe(AudioInput(samples), "en"),
        await _streamed(engine, samples),
    ):
        assert (transcript.text, transcript.language, transcript.segments) == ("", "en", ())
        assert transcript.duration_s == len(samples) / SR
    assert fake[0].calls == []


async def _streamed(engine: WhisperEngine, samples: NDArray[np.float32]) -> Any:
    async with await engine.stream("en") as session:
        for piece in np.array_split(samples, 10):
            await session.add_audio(piece)
        return await session.finalize()


async def test_bad_input_is_the_clients_mistake(fake: tuple[_Model, list[str]]) -> None:
    engine = await _loaded(fake)
    for samples in (np.zeros((2, SR), np.float32), np.zeros(SR, np.int16), np.full(SR, np.nan)):
        with pytest.raises(EngineError):
            await engine.transcribe(AudioInput(samples))  # type: ignore[arg-type]
    with pytest.raises(EngineError, match="8000 Hz"):
        await engine.transcribe(AudioInput(SPEECH, sample_rate=8000))


async def test_a_stream_transcribes_the_turn_at_finalize_with_the_lock_per_span(
    fake: tuple[_Model, list[str]],
) -> None:
    model, _ = fake
    engine = await _loaded(fake)
    async with await engine.stream("de") as session:
        for piece in np.array_split(_gaps(SPEECH, 5.0, SPEECH), 30):
            await session.add_audio(piece)
            assert session.partial() == ""
        assert model.calls == []
        transcript = await session.finalize()
    assert transcript.language == "de"
    assert [(c["language"], c["word_timestamps"]) for c in model.calls] == [("de", False)] * 2
    assert model.locked == [True, True]
    assert model.turns.log == ["enter", "exit"] * 2  # once a span, not around the turn


async def test_a_cancelled_finalize_stops_between_spans(fake: tuple[_Model, list[str]]) -> None:
    """A client that hangs up during finalize cancels it: the span being
    decoded finishes, the later ones are not decoded."""
    model, _ = fake
    engine = await _loaded(fake)
    with blocking_first_call(model) as started:
        session = await engine.stream("en")
        await session.add_audio(_gaps(SPEECH, 3.0, SPEECH))
        task = asyncio.create_task(session.finalize())
        await started()
        task.cancel()
        await until(session._stop.is_set)  # the cancel reached the session
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(model.calls) == 1
    assert engine._session is None
    assert model.turns.log == ["enter", "exit"]


async def test_an_empty_stream_is_an_empty_transcript(fake: tuple[_Model, list[str]]) -> None:
    engine = await _loaded(fake)
    empty = await (await engine.stream()).finalize()
    assert (empty.text, empty.duration_s) == ("", 0.0)
    assert fake[0].calls == [] and fake[0].turns.log == []


async def test_one_session_at_a_time_and_no_batch_meanwhile(fake: tuple[_Model, list[str]]) -> None:
    engine = await _loaded(fake)
    session = await engine.stream()
    with pytest.raises(EngineError, match="session is open"):
        await engine.stream()
    with pytest.raises(EngineError, match="session is open"):
        await engine.transcribe(AudioInput(SPEECH))
    await session.finalize()
    await engine.transcribe(AudioInput(SPEECH))  # free again


# --- against the real models ------------------------------------------------


@pytest.fixture(scope="module")
def whisper_engine() -> Iterator[WhisperEngine]:
    """Loaded synchronously so it can be module scoped regardless of the
    per-test event loop; every call runs on the engine's own thread."""
    engine = WhisperEngine()
    asyncio.run(engine.load())
    yield engine


@pytest.mark.model
def test_transcribes_with_word_timed_segments(whisper_engine: WhisperEngine) -> None:
    reference = "Turn on the kitchen lights and set the thermostat to twenty degrees."
    for hint in ("en", None):
        transcript = asyncio.run(whisper_engine.transcribe(AudioInput(SPEECH), hint))
        assert word_accuracy(transcript.text, reference) >= 0.9, transcript.text
        assert transcript.language == "en"
        assert transcript.segments, transcript
        ends = [s.end for s in transcript.segments]
        assert 0 <= transcript.segments[0].start < ends[0] <= ends[-1] <= len(SPEECH) / SR
        assert " ".join(s.text for s in transcript.segments) == transcript.text


@pytest.mark.model
@pytest.mark.parametrize("samples", [np.zeros(2 * SR, np.float32), HUM], ids=["silence", "hum"])
def test_no_speech_is_an_empty_transcript(
    whisper_engine: WhisperEngine, samples: NDArray[np.float32]
) -> None:
    transcript = asyncio.run(whisper_engine.transcribe(AudioInput(samples)))
    assert transcript.text == ""


@pytest.mark.model
def test_a_stream_gives_the_batch_transcript_at_finalize(whisper_engine: WhisperEngine) -> None:
    """Fed in 64 ms chunks; prints the finalize latency, with and without a hint."""
    speech = load_fixture("speech_en_trailing_silence.wav")

    async def turn(hint: str | None) -> tuple[str, float]:
        async with await whisper_engine.stream(hint) as session:
            for start in range(0, len(speech), 1024):
                await session.add_audio(speech[start : start + 1024])
                assert session.partial() == ""
            t0 = time.perf_counter()
            transcript = await session.finalize()
            return transcript.text, (time.perf_counter() - t0) * 1000

    batch = asyncio.run(whisper_engine.transcribe(AudioInput(speech), "en"))
    assert word_accuracy(batch.text, "What is the weather like tomorrow morning?") >= 0.9
    for hint in ("en", None):
        runs = [asyncio.run(turn(hint)) for _ in range(5)]
        assert {text for text, _ in runs} == {batch.text}
        latencies = [ms for _, ms in runs]
        median, worst = statistics.median(latencies), max(latencies)
        print(f"\nfinalize, 2.8 s turn, hint {hint}: median {median:.0f} ms, worst {worst:.0f} ms")


@pytest.mark.model
def test_spanish_is_detected(whisper_engine: WhisperEngine) -> None:
    speech = np.concatenate([np.zeros(SR, np.float32), load_fixture("speech_es.wav")])
    reference = "Enciende las luces de la cocina y pon la calefacción a veinte grados."
    transcript = asyncio.run(whisper_engine.transcribe(AudioInput(speech)))
    assert transcript.language == "es"
    assert word_accuracy(transcript.text, reference) >= 0.8, transcript.text
    assert 0.6 < transcript.segments[0].start < 1.4  # word times, not the window's start


@pytest.mark.model
def test_an_unknown_language_is_refused(whisper_engine: WhisperEngine) -> None:
    with pytest.raises(UnsupportedLanguage):
        asyncio.run(whisper_engine.transcribe(AudioInput(SPEECH), "xx"))


@pytest.mark.model
def test_the_language_table_is_the_tokenizers() -> None:
    from mlx_audio.stt.models.whisper.tokenizer import LANGUAGES as CODES

    model_codes = [whisper.JAVANESE[1] if c == whisper.JAVANESE[0] else c for c in LANGUAGES]
    assert model_codes == list(CODES)


@pytest.mark.model
@pytest.mark.parametrize("model_id", CATALOGUED)
def test_each_catalogued_model_is_downloaded(model_id: str) -> None:
    assert models.snapshot(model_id, check_only=True).is_dir()


def _noise(seconds: float, dbfs: float = -50.0) -> NDArray[np.float32]:
    rng = np.random.default_rng(0)
    return (rng.standard_normal(round(seconds * SR)) * 10 ** (dbfs / 20)).astype(np.float32)


@pytest.mark.model
def test_long_noise_between_turns_writes_no_words(whisper_engine: WhisperEngine) -> None:
    """Speech, 40 s of room noise, speech, 25 s of noise: the sentence twice,
    batch and stream, nothing written for the noise."""
    samples = np.concatenate([SPEECH, _noise(40.0), SPEECH, _noise(25.0)])
    batch = asyncio.run(whisper_engine.transcribe(AudioInput(samples), "en"))

    async def streamed() -> Any:
        async with await whisper_engine.stream("en") as session:
            await session.add_audio(samples)
            return await session.finalize()

    for transcript in (batch, asyncio.run(streamed())):
        reference = "Turn on the kitchen lights and set the thermostat to twenty degrees."
        assert word_accuracy(transcript.text, f"{reference} {reference}") >= 0.9, transcript.text
    assert batch.segments[-1].end < len(SPEECH) / SR + 40.0 + len(SPEECH) / SR + 1.0


@pytest.mark.model
def test_spanish_after_35_s_of_noise_is_detected(whisper_engine: WhisperEngine) -> None:
    samples = np.concatenate([_noise(35.0), load_fixture("speech_es.wav")])
    transcript = asyncio.run(whisper_engine.transcribe(AudioInput(samples)))
    assert transcript.language == "es"
    reference = "Enciende las luces de la cocina y pon la calefacción a veinte grados."
    assert word_accuracy(transcript.text, reference) >= 0.8, transcript.text


@pytest.fixture
def stt_engine() -> WhisperEngine:
    """The engine the ``stt_client`` app loads (``tests/conftest.py``)."""
    return WhisperEngine()


@pytest.mark.model
async def test_models_and_a_transcription_over_http(stt_client: AsyncClient) -> None:
    (model,) = (await stt_client.get("/v1/models")).json()["data"]
    assert (model["id"], model["engine"], model["license"]) == (CATALOGUED[0], "whisper", "MIT")
    wav = (Path(__file__).parent.parent / "fixtures" / "speech_en.wav").read_bytes()
    response = await stt_client.post(
        "/v1/audio/transcriptions",
        files={"file": ("speech.wav", wav, "audio/wav")},
        data={"response_format": "verbose_json"},
    )
    body = response.json()
    assert response.status_code == 200, body
    assert body["language"] == "en" and body["segments"]


@pytest.mark.model
def test_the_fp16_model_loads_and_transcribes() -> None:
    engine = WhisperEngine(CATALOGUED[1])
    asyncio.run(engine.load())
    transcript = asyncio.run(engine.transcribe(AudioInput(SPEECH), "en"))
    reference = "Turn on the kitchen lights and set the thermostat to twenty degrees."
    assert word_accuracy(transcript.text, reference) >= 0.9, transcript.text
