"""Qwen3-ASR engine tests: first with stand-in ``mlx`` and ``mlx_audio``
modules (they run on CI), then against the real models (``model``):
``VOICED_MODEL_TESTS=1 uv run pytest tests/stt/test_qwen3_asr.py -m model -s``."""

from __future__ import annotations

import asyncio
import json
import logging
import statistics
import subprocess
import sys
import time
import types
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest
from httpx import AsyncClient
from numpy.typing import NDArray

from tests.stt.conftest import load_fixture, word_accuracy
from voiced import models
from voiced.engines import qwen3_asr
from voiced.engines.base import (
    AudioInput,
    EngineError,
    EngineNotLoaded,
    Segment,
    UnsupportedLanguage,
)
from voiced.engines.catalogue import engines_list
from voiced.engines.qwen3_asr import ALIGNER_REPO, LANGUAGES, Qwen3ASREngine
from voiced.engines.registry import ModelRefused

SR = 16_000
SPEECH = np.full(3 * SR, 0.1, dtype=np.float32)
"""Three seconds the stand-in model hears as speech."""
CATALOGUED = [m.id for m in models.engine("qwen3-asr").models]  # type: ignore[union-attr]


def test_registered_with_its_languages_and_one_stream() -> None:
    engine = Qwen3ASREngine()
    assert engine.languages[0] == "en" and len(engine.languages) == 30
    assert engine.max_streams == 1
    stt = {e["name"]: e for e in engines_list()["stt"]}
    assert stt["qwen3-asr"]["models"] == CATALOGUED


def test_its_models_and_the_aligner_are_catalogued() -> None:
    assert CATALOGUED == ["mlx-community/Qwen3-ASR-0.6B-8bit", "mlx-community/Qwen3-ASR-1.7B-8bit"]
    assert models.is_shared(ALIGNER_REPO)


def test_a_qwen3_asr_checkpoint_outside_the_catalogue_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ModelRefused, match=r"are mlx-community/Qwen3-ASR-0\.6B-8bit"):
        Qwen3ASREngine("mlx-community/Qwen3-ASR-1.7B-4bit")
    assert Qwen3ASREngine(str(tmp_path)).model_id == str(tmp_path)  # a local model is not


def test_the_module_imports_without_mlx_audio() -> None:
    """The registry imports every engine module; mlx-audio is imported in load()."""
    code = "import sys; sys.modules['mlx_audio'] = None; import voiced.engines.qwen3_asr"
    subprocess.run([sys.executable, "-c", code], check=True)


@pytest.mark.parametrize("code", list(LANGUAGES))
def test_each_language_round_trips(code: str) -> None:
    assert Qwen3ASREngine.normalize_language(code) == code
    assert Qwen3ASREngine.normalize_language(f"{code.upper()}-XX") == code
    assert qwen3_asr._code(LANGUAGES[code]) == code
    assert qwen3_asr._code(LANGUAGES[code].lower()) == code


@pytest.mark.parametrize(
    "hint, code", [(None, None), ("EN-us", "en"), ("pt_BR", "pt"), (" de ", "de")]
)
def test_language_hints_are_bcp47(hint: str | None, code: str | None) -> None:
    assert Qwen3ASREngine.normalize_language(hint) == code


@pytest.mark.parametrize("hint", ["xx", "bg", "tl", ""])
def test_a_language_the_model_does_not_know_is_refused(hint: str) -> None:
    with pytest.raises(UnsupportedLanguage):
        Qwen3ASREngine.normalize_language(hint)


# --- stand-ins ------------------------------------------------------------


class _Model:
    """Stands in for mlx-audio's Qwen3-ASR model, called once per piece: it
    writes the next of ``texts`` ("LOOP" writes until the token bound) in the
    language given, else in the next of ``detects``."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.texts = ["Hello there. How are you?"]
        self.detects = ["English"]
        self.locked: list[bool] = []

    def generate(self, samples: NDArray[np.float32], **kwargs: Any) -> Any:
        self.calls.append({"samples": len(samples), **kwargs})
        self.locked.append(_HELD[0])
        n = len(self.calls) - 1
        text = self.texts[n % len(self.texts)]
        if text == "LOOP":
            text = " ".join(["la"] * kwargs["max_tokens"])
        language = kwargs["language"] or self.detects[n % len(self.detects)]
        segment = {"text": text, "language": language, "start": 0.0, "end": len(samples) / SR}
        return SimpleNamespace(text=text, segments=[segment])


class _Aligner:
    """Stands in for the forced aligner: one word per 0.5 s from 0.25 s
    (times ``stretch``)."""

    def __init__(self) -> None:
        self.calls: list[tuple[int, str, str]] = []
        self.drop = 0
        self.stretch = 1.0
        self.languages = ["english", "chinese", "japanese"]
        self.aligner_processor = SimpleNamespace(encode_timestamp=lambda s, lang: (s.split(), ""))

    def get_supported_languages(self) -> list[str]:
        return self.languages

    def generate(self, audio: NDArray[np.float32], *, text: str, language: str) -> Any:
        self.calls.append((len(audio), text, language))
        words = text.split()[: len(text.split()) - self.drop]
        k = self.stretch
        items = [
            SimpleNamespace(text=w, start_time=(0.25 + 0.5 * i) * k, end_time=(0.65 + 0.5 * i) * k)
            for i, w in enumerate(words)
        ]
        return SimpleNamespace(items=items)


_HELD = [False]
_TURNS: list[str] = []  # each enter and exit of the GPU turn lock


@contextmanager
def _turn() -> Iterator[None]:
    _TURNS.append("enter")
    _HELD[0] = True
    try:
        yield
    finally:
        _HELD[0] = False
        _TURNS.append("exit")


def _split(samples: NDArray[np.float32], *, sr: int, chunk_duration: float) -> list[Any]:
    """mlx-audio's splitter without the search for a quiet cut."""
    n = round(chunk_duration * sr)
    return [(samples[i : i + n], i / sr) for i in range(0, len(samples), n)]


@pytest.fixture
def fakes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[_Model, _Aligner, list[str]]:
    """``mlx.core``, ``mlx_audio``'s loader and splitter, and the snapshots;
    nagisa stands for a word tokenizer that is not installed."""
    model, aligner, loaded = _Model(), _Aligner(), []

    def load_model(path: Path) -> Any:
        loaded.append(str(path.relative_to(tmp_path)))
        return aligner if "Aligner" in str(path) else model

    core = types.ModuleType("mlx.core")
    core.clear_cache = lambda: None  # type: ignore[attr-defined]
    mlx = types.ModuleType("mlx")
    mlx.core = core  # type: ignore[attr-defined]
    utils = types.ModuleType("mlx_audio.stt.utils")
    utils.load_model = load_model  # type: ignore[attr-defined]
    splitter = types.ModuleType("mlx_audio.stt.models.qwen3_asr.qwen3_asr")
    splitter.split_audio_into_chunks = _split  # type: ignore[attr-defined]
    for name, module in {
        "mlx": mlx,
        "mlx.core": core,
        "mlx_audio": types.ModuleType("mlx_audio"),
        "mlx_audio.stt": types.ModuleType("mlx_audio.stt"),
        "mlx_audio.stt.utils": utils,
        "mlx_audio.stt.models": types.ModuleType("mlx_audio.stt.models"),
        "mlx_audio.stt.models.qwen3_asr": types.ModuleType("mlx_audio.stt.models.qwen3_asr"),
        "mlx_audio.stt.models.qwen3_asr.qwen3_asr": splitter,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setattr(models, "snapshot", lambda repo, check_only: tmp_path / repo)
    monkeypatch.setattr(qwen3_asr, "stt_turn", _turn)
    _TURNS.clear()
    monkeypatch.setitem(qwen3_asr._TOKENIZERS, "japanese", "voiced_no_such_tokenizer")
    return model, aligner, loaded


async def _loaded(fakes: tuple[_Model, _Aligner, list[str]]) -> Qwen3ASREngine:
    engine = Qwen3ASREngine()
    await engine.load()
    fakes[0].calls.clear()
    fakes[0].locked.clear()
    fakes[1].calls.clear()
    return engine


# --- with the stand-ins -----------------------------------------------------


async def test_load_reads_both_pinned_snapshots_and_warms_both(
    fakes: tuple[_Model, _Aligner, list[str]],
) -> None:
    model, aligner, loaded = fakes
    await Qwen3ASREngine().load()
    assert loaded == [Qwen3ASREngine.model_id, ALIGNER_REPO]
    assert [c["language"] for c in model.calls] == ["English"]
    assert [c[1:] for c in aligner.calls] == [("hello", "English")]


async def test_a_load_that_fails_leaves_it_unloaded(
    fakes: tuple[_Model, _Aligner, list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("no Metal")

    monkeypatch.setattr(fakes[1], "generate", broken)
    engine = Qwen3ASREngine()
    with pytest.raises(RuntimeError, match="no Metal"):
        await engine.load()
    with pytest.raises(EngineNotLoaded):
        await engine.transcribe(AudioInput(SPEECH))


async def test_a_missing_aligner_is_the_models_download_naming_it(
    fakes: tuple[_Model, _Aligner, list[str]], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def snapshot(repo: str, check_only: bool) -> Path:
        if repo == ALIGNER_REPO:
            raise models.ModelNotDownloaded(repo, models.pinned_revision(repo))
        return tmp_path / repo

    monkeypatch.setattr(models, "snapshot", snapshot)
    engine = Qwen3ASREngine()
    with pytest.raises(models.ModelNotDownloaded) as raised:
        await engine.load()
    assert (raised.value.model_id, raised.value.missing) == (engine.model_id, (ALIGNER_REPO,))
    assert raised.value.command == f"voiced models download {engine.model_id}"
    assert fakes[2] == []  # nothing loaded
    with pytest.raises(EngineNotLoaded):
        await engine.transcribe(AudioInput(SPEECH))


async def test_a_hint_is_passed_by_the_models_name_with_a_token_bound(
    fakes: tuple[_Model, _Aligner, list[str]],
) -> None:
    model, _, _ = fakes
    engine = await _loaded(fakes)
    transcript = await engine.transcribe(AudioInput(SPEECH), "de-AT")
    assert transcript.language == "de"
    (call,) = model.calls
    assert call == {
        "samples": len(SPEECH),
        "language": "German",
        "chunk_duration": float("inf"),  # one piece, already split
        "max_tokens": qwen3_asr.MIN_TOKENS,  # three seconds need less
    }
    long = np.full(10 * SR, 0.1, dtype=np.float32)
    await engine.transcribe(AudioInput(long))
    assert model.calls[-1]["max_tokens"] == 10 * qwen3_asr.TOKENS_PER_S


@pytest.mark.parametrize("name, code", [("Chinese", "zh"), ("cantonese", "yue"), ("Klingon", None)])
async def test_the_detected_language_comes_back_as_bcp47(
    fakes: tuple[_Model, _Aligner, list[str]], name: str, code: str | None
) -> None:
    fakes[0].detects = [name]
    engine = await _loaded(fakes)
    transcript = await engine.transcribe(AudioInput(SPEECH))
    assert fakes[0].calls[0]["language"] is None
    assert transcript.language == code


async def test_each_piece_has_its_own_token_bound_and_language(
    fakes: tuple[_Model, _Aligner, list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A piece that loops stops at its own bound and the next is still
    transcribed; a piece of music detects nothing and the next detects
    German, not the first piece's language for the whole file."""
    model, _, _ = fakes
    monkeypatch.setattr(qwen3_asr, "CHUNK_S", 5.0)
    model.texts = ["LOOP", "Guten Tag."]
    engine = await _loaded(fakes)
    samples = np.full(10 * SR, 0.1, dtype=np.float32)
    transcript = await engine.transcribe(AudioInput(samples), "de")
    assert [c["max_tokens"] for c in model.calls] == [5 * qwen3_asr.TOKENS_PER_S] * 2
    assert transcript.text.endswith("la Guten Tag.")
    model.calls.clear()
    model.texts, model.detects = ["", "Guten Tag."], ["None", "German"]
    transcript = await engine.transcribe(AudioInput(samples))
    assert [c["language"] for c in model.calls] == [None, None]
    assert (transcript.text, transcript.language) == ("Guten Tag.", "de")
    assert transcript.segments == (Segment(5.0, 10.0, "Guten Tag."),)  # not aligned in German


async def test_batch_segments_are_sentences_with_the_aligners_times(
    fakes: tuple[_Model, _Aligner, list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    model, aligner, _ = fakes
    monkeypatch.setattr(qwen3_asr, "CHUNK_S", 3.0)
    model.texts = ["Hello there. How are you?", " Fine,  thanks. "]
    engine = await _loaded(fakes)
    samples = np.full(5 * SR, 0.1, dtype=np.float32)
    transcript = await engine.transcribe(AudioInput(samples), "en")
    assert transcript.text == "Hello there. How are you? Fine, thanks."
    assert transcript.segments == (
        Segment(0.25, 1.15, "Hello there."),
        Segment(1.25, 2.65, "How are you?"),
        Segment(3.25, 4.15, "Fine, thanks."),
    )
    assert [(n, t) for n, t, _ in aligner.calls] == [
        (3 * SR, "Hello there. How are you?"),  # each piece's own audio
        (2 * SR, "Fine, thanks."),
    ]
    assert model.locked == [False, False]  # batch does not hold the GPU lock


async def test_a_silent_piece_never_reaches_the_model(
    fakes: tuple[_Model, _Aligner, list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    model, _, _ = fakes
    monkeypatch.setattr(qwen3_asr, "CHUNK_S", 3.0)
    model.texts = ["Hello there.", "How are you?"]
    engine = await _loaded(fakes)
    samples = np.concatenate([SPEECH, np.zeros(3 * SR, np.float32), SPEECH])
    transcript = await engine.transcribe(AudioInput(samples), "fil")
    assert len(model.calls) == 2
    assert [(s.start, s.end) for s in transcript.segments] == [(0.0, 3.0), (6.0, 9.0)]


async def test_a_closed_session_stops_between_pieces(
    fakes: tuple[_Model, _Aligner, list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cancelled finalize closes its session; the turn's later pieces are
    then not decoded."""
    model, _, _ = fakes
    monkeypatch.setattr(qwen3_asr, "CHUNK_S", 1.0)
    engine = await _loaded(fakes)
    transcript = engine._finalize_blocking([SPEECH], "en", lambda: len(model.calls) >= 1)
    assert len(model.calls) == 1
    assert transcript.text == "Hello there. How are you?"


async def test_aligned_times_stay_inside_their_piece(
    fakes: tuple[_Model, _Aligner, list[str]],
) -> None:
    fakes[1].stretch = 10.0
    engine = await _loaded(fakes)
    transcript = await engine.transcribe(AudioInput(SPEECH), "en")
    assert [s.text for s in transcript.segments] == ["Hello there.", "How are you?"]
    assert all(0.0 <= s.start <= s.end <= 3.0 for s in transcript.segments)


@pytest.mark.parametrize("hint", ["fil", "ja"])
async def test_a_language_the_aligner_cannot_align_gets_one_segment_per_piece(
    fakes: tuple[_Model, _Aligner, list[str]], hint: str
) -> None:
    """Filipino is not the aligner's; Japanese is, but needs a word
    tokenizer that is not installed (nagisa)."""
    engine = await _loaded(fakes)
    transcript = await engine.transcribe(AudioInput(SPEECH), hint)
    assert transcript.segments == (Segment(0.0, 3.0, "Hello there. How are you?"),)
    assert fakes[1].calls == []


async def test_a_failed_alignment_keeps_the_transcript_as_one_segment(
    fakes: tuple[_Model, _Aligner, list[str]],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    engine = await _loaded(fakes)

    def broken(*args: Any, **kwargs: Any) -> Any:
        raise ImportError("Japanese tokenization requires nagisa")

    monkeypatch.setattr(fakes[1], "generate", broken)
    with caplog.at_level(logging.WARNING, logger=qwen3_asr.__name__):
        transcript = await engine.transcribe(AudioInput(SPEECH), "en")
    assert transcript.segments == (Segment(0.0, 3.0, "Hello there. How are you?"),)
    assert "aligning a English piece failed" in caplog.text


async def test_aligned_words_that_do_not_match_the_text_give_one_segment(
    fakes: tuple[_Model, _Aligner, list[str]],
) -> None:
    fakes[1].drop = 1
    engine = await _loaded(fakes)
    transcript = await engine.transcribe(AudioInput(SPEECH), "en")
    assert transcript.segments == (Segment(0.0, 3.0, "Hello there. How are you?"),)


@pytest.mark.parametrize(
    "samples",
    [np.zeros(3 * SR, dtype=np.float32), np.full(800, 0.1, dtype=np.float32)],
    ids=["silence", "too-short"],
)
async def test_silence_and_slivers_never_reach_the_model(
    fakes: tuple[_Model, _Aligner, list[str]], samples: NDArray[np.float32]
) -> None:
    engine = await _loaded(fakes)
    transcript = await engine.transcribe(AudioInput(samples), "en")
    assert (transcript.text, transcript.language, transcript.segments) == ("", "en", ())
    assert transcript.duration_s == len(samples) / SR
    assert fakes[0].calls == []


async def test_bad_input_is_the_clients_mistake(
    fakes: tuple[_Model, _Aligner, list[str]],
) -> None:
    engine = await _loaded(fakes)
    for samples in (np.zeros((2, SR), np.float32), np.zeros(SR, np.int16), np.full(SR, np.nan)):
        with pytest.raises(EngineError):
            await engine.transcribe(AudioInput(samples))  # type: ignore[arg-type]
    with pytest.raises(EngineError, match="8000 Hz"):
        await engine.transcribe(AudioInput(SPEECH, sample_rate=8000))


async def test_a_stream_buffers_and_transcribes_the_turn_at_finalize(
    fakes: tuple[_Model, _Aligner, list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    model, aligner, _ = fakes
    monkeypatch.setattr(qwen3_asr, "CHUNK_S", 2.0)
    model.texts = ["Hello there.", "How are you?"]
    engine = await _loaded(fakes)
    async with await engine.stream("en-GB") as session:
        for piece in np.array_split(SPEECH, 30):
            await session.add_audio(piece)
            assert session.partial() == ""
        assert model.calls == []
        transcript = await session.finalize()
    assert transcript.text == "Hello there. How are you?"
    assert transcript.language == "en"
    # One segment a piece, no aligner, and each piece under the GPU lock.
    assert transcript.segments == (
        Segment(0.0, 2.0, "Hello there."),
        Segment(2.0, 3.0, "How are you?"),
    )
    assert [c["samples"] for c in model.calls] == [2 * SR, SR]
    assert model.locked == [True, True]
    assert _TURNS == ["enter", "exit"] * 2  # once a piece, not around the turn
    assert aligner.calls == []


async def test_one_session_at_a_time_and_no_batch_meanwhile(
    fakes: tuple[_Model, _Aligner, list[str]],
) -> None:
    engine = await _loaded(fakes)
    session = await engine.stream()
    with pytest.raises(EngineError, match="session is open"):
        await engine.stream()
    with pytest.raises(EngineError, match="session is open"):
        await engine.transcribe(AudioInput(SPEECH))
    await session.finalize()
    with pytest.raises(EngineError, match="closed"):
        await session.add_audio(SPEECH)
    with pytest.raises(EngineError, match="closed"):
        await session.finalize()
    await engine.transcribe(AudioInput(SPEECH))  # free again


async def test_closing_without_finalize_discards_the_turn(
    fakes: tuple[_Model, _Aligner, list[str]],
) -> None:
    engine = await _loaded(fakes)
    async with await engine.stream() as session:
        await session.add_audio(SPEECH)
    await session.close()  # idempotent
    assert fakes[0].calls == []
    empty = await (await engine.stream()).finalize()
    assert (empty.text, empty.duration_s) == ("", 0.0)


async def test_a_failed_finalize_closes_the_session(
    fakes: tuple[_Model, _Aligner, list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = await _loaded(fakes)
    session = await engine.stream()
    await session.add_audio(SPEECH)

    def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("decoder fell over")

    monkeypatch.setattr(fakes[0], "generate", broken)
    with pytest.raises(RuntimeError, match="fell over"):
        await session.finalize()
    with pytest.raises(EngineError, match="closed"):
        await session.add_audio(SPEECH)
    assert engine._session is None


# --- against the real models ------------------------------------------------


@pytest.fixture(scope="module")
def qwen3_engine() -> Iterator[Qwen3ASREngine]:
    """Loaded synchronously so it can be module scoped regardless of the
    per-test event loop; every call runs on the engine's own thread."""
    engine = Qwen3ASREngine()
    asyncio.run(engine.load())
    yield engine


@pytest.mark.model
def test_transcribes_with_sentence_segments(qwen3_engine: Qwen3ASREngine) -> None:
    speech = load_fixture("speech_en.wav")
    reference = "Turn on the kitchen lights and set the thermostat to twenty degrees."
    for hint in ("en", None):
        transcript = asyncio.run(qwen3_engine.transcribe(AudioInput(speech), hint))
        assert word_accuracy(transcript.text, reference) >= 0.9, transcript.text
        assert transcript.language == "en"
        assert transcript.segments, transcript
        ends = [s.end for s in transcript.segments]
        assert 0 <= transcript.segments[0].start < ends[0] <= ends[-1] <= len(speech) / SR + 0.05
        assert " ".join(s.text for s in transcript.segments) == transcript.text


@pytest.mark.model
def test_silence_is_an_empty_transcript(qwen3_engine: Qwen3ASREngine) -> None:
    transcript = asyncio.run(qwen3_engine.transcribe(AudioInput(np.zeros(2 * SR, np.float32))))
    assert transcript.text == ""


@pytest.mark.model
def test_a_stream_gives_the_batch_transcript_at_finalize(qwen3_engine: Qwen3ASREngine) -> None:
    """Fed in 64 ms chunks; prints the finalize latency (one decode)."""
    speech = load_fixture("speech_en_trailing_silence.wav")

    async def turn() -> tuple[str, float]:
        async with await qwen3_engine.stream("en") as session:
            for start in range(0, len(speech), 1024):
                await session.add_audio(speech[start : start + 1024])
                assert session.partial() == ""
            t0 = time.perf_counter()
            transcript = await session.finalize()
            return transcript.text, (time.perf_counter() - t0) * 1000

    runs = [asyncio.run(turn()) for _ in range(5)]
    batch = asyncio.run(qwen3_engine.transcribe(AudioInput(speech), "en"))
    assert {text for text, _ in runs} == {batch.text}
    assert word_accuracy(batch.text, "What is the weather like tomorrow morning?") >= 0.9
    latencies = [ms for _, ms in runs]
    median, worst = statistics.median(latencies), max(latencies)
    print(f"\nfinalize, 2.8 s turn: median {median:.0f} ms, worst {worst:.0f} ms")


@pytest.mark.model
def test_an_unknown_language_is_refused(qwen3_engine: Qwen3ASREngine) -> None:
    with pytest.raises(UnsupportedLanguage):
        asyncio.run(qwen3_engine.transcribe(AudioInput(SPEECH), "bg"))


@pytest.mark.model
@pytest.mark.parametrize("model_id", CATALOGUED)
def test_the_language_table_is_the_checkpoints(model_id: str) -> None:
    config = models.snapshot(model_id, check_only=True) / "config.json"
    names = json.loads(config.read_text())["support_languages"]
    assert sorted(names) == sorted(LANGUAGES.values())


@pytest.mark.model
def test_spanish_is_detected_and_aligned(qwen3_engine: Qwen3ASREngine) -> None:
    # A second of silence first: one segment per piece would start at 0.
    speech = np.concatenate([np.zeros(SR, np.float32), load_fixture("speech_es.wav")])
    reference = "Enciende las luces de la cocina y pon la calefacción a veinte grados."
    transcript = asyncio.run(qwen3_engine.transcribe(AudioInput(speech)))
    assert transcript.language == "es"
    assert word_accuracy(transcript.text, reference) >= 0.8, transcript.text
    (segment,) = transcript.segments  # one sentence, timed by the aligner
    assert 0.8 < segment.start < 1.2 < segment.end <= len(speech) / SR


@pytest.fixture
def stt_engine() -> Qwen3ASREngine:
    """The engine the ``stt_client`` app loads (``tests/conftest.py``)."""
    return Qwen3ASREngine()


@pytest.mark.model
async def test_models_and_a_transcription_over_http(stt_client: AsyncClient) -> None:
    (model,) = (await stt_client.get("/v1/models")).json()["data"]
    assert (model["id"], model["engine"], model["license"]) == (
        "mlx-community/Qwen3-ASR-0.6B-8bit",
        "qwen3-asr",
        "Apache-2.0",
    )
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
def test_the_larger_model_loads_and_transcribes() -> None:
    engine = Qwen3ASREngine("mlx-community/Qwen3-ASR-1.7B-8bit")
    speech = load_fixture("speech_en.wav")

    async def run() -> str:
        await engine.load()
        return (await engine.transcribe(AudioInput(speech))).text

    reference = "Turn on the kitchen lights and set the thermostat to twenty degrees."
    assert word_accuracy(asyncio.run(run()), reference) >= 0.9
