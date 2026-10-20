"""Chatterbox Turbo engine tests. The shared contract tests are in
``test_sentence_engines.py``; these cover what is Chatterbox's own, first
without MLX (they run on CI), then against the real model (``model``)."""

from __future__ import annotations

import asyncio
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import huggingface_hub
import numpy as np
import pytest
from httpx import AsyncClient
from numpy.typing import NDArray

from voiced import models
from voiced.engines import chatterbox
from voiced.engines.base import TTS_SAMPLE_RATE, UnknownVoice
from voiced.engines.chatterbox import (
    CHUNK_TOKENS,
    DEFAULT_VOICE,
    FIRST_TOKENS,
    MODELS,
    S3_TOKENIZER_REPO,
    SILENCE_TOKEN,
    SILENCE_TOKENS,
    ChatterboxEngine,
    SpeechTokenizerSkipped,
    max_tokens,
    speak,
)
from voiced.engines.registry import get_tts_engine


def test_registered_with_its_model_and_licence() -> None:
    assert get_tts_engine("chatterbox") is ChatterboxEngine
    engine = ChatterboxEngine()
    assert (engine.model_id, engine.license) == ("mlx-community/chatterbox-turbo-4bit", "MIT")
    assert ChatterboxEngine.catalogue_voices() == [DEFAULT_VOICE]


def test_both_models_are_pinned_with_the_default_voice() -> None:
    assert MODELS == ("mlx-community/chatterbox-turbo-4bit", "mlx-community/chatterbox-turbo-fp16")
    for model in MODELS:
        entry = models.locked(model)
        assert entry is not None and "conds.safetensors" in entry.files
    assert models.find(S3_TOKENIZER_REPO) is None  # never downloaded


def test_the_module_imports_without_mlx() -> None:
    """The registry imports every engine module; MLX is imported in load()."""
    code = (
        "import sys; sys.modules['mlx'] = None; sys.modules['mlx_audio'] = None; "
        "import voiced.engines.chatterbox"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


def test_a_sentence_is_bounded_by_its_length() -> None:
    assert max_tokens("Hi.") == 34  # (3 * 0.12 + 1) s at 25 tokens a second
    assert max_tokens("x" * 100) == 325


# -- the streaming schedule ----------------------------------------------------


class _Run:
    """A sentence of ``n`` tokens from a stand-in T3 and S3Gen, with every
    wait, step and render recorded in order."""

    def __init__(self, n: int, *, last_flag: bool = True) -> None:
        self.n = n
        self.last_flag = last_flag
        self.log: list[str] = []
        self.renders: list[tuple[int, int, bool]] = []

    def steps(self) -> Iterator[tuple[list[int], bool]]:
        for i in range(self.n):
            self.log.append("step")
            yield [i], self.last_flag and i == self.n - 1

    def render(self, tokens: list[int], done: int, final: bool) -> tuple[NDArray[np.float32], int]:
        self.log.append("render")
        self.renders.append((len(tokens), done, final))
        total = len(tokens) * 10
        return np.ones(total - done, dtype=np.float32), total

    def wait(self) -> None:
        self.log.append("wait")

    def pieces(self) -> list[NDArray[np.float32]]:
        return list(speak(self.steps(), self.render, self.wait))


def test_renders_the_first_tokens_early_then_in_larger_chunks() -> None:
    run = _Run(FIRST_TOKENS + 2 * CHUNK_TOKENS + 3)
    pieces = run.pieces()
    final = run.n + SILENCE_TOKENS
    assert run.renders == [
        (FIRST_TOKENS, 0, False),
        (FIRST_TOKENS + CHUNK_TOKENS, FIRST_TOKENS * 10, False),
        (FIRST_TOKENS + 2 * CHUNK_TOKENS, (FIRST_TOKENS + CHUNK_TOKENS) * 10, False),
        (final, (FIRST_TOKENS + 2 * CHUNK_TOKENS) * 10, True),
    ]
    # Each piece is only the audio not yet sent: together, the whole sentence.
    assert sum(len(p) for p in pieces) == final * 10


def test_waits_for_the_stt_before_every_gpu_call() -> None:
    run = _Run(FIRST_TOKENS + 1)
    run.pieces()
    calls = [c for c in run.log if c != "wait"]
    assert calls == ["step"] * FIRST_TOKENS + ["render", "step", "render"]
    for i, call in enumerate(run.log):
        if call != "wait":
            assert run.log[i - 1] == "wait", run.log


def test_the_final_render_adds_silence_tokens() -> None:
    tokens: list[list[int]] = []
    run = _Run(3)
    run.render = lambda t, done, final: (tokens.append(list(t)), (np.zeros(0, np.float32), 0))[1]  # type: ignore[method-assign]
    run.pieces()
    assert tokens == [[0, 1, 2] + [SILENCE_TOKEN] * SILENCE_TOKENS]


def test_a_stream_that_ends_without_a_final_flag_still_finalizes() -> None:
    """T3 stops without flagging the last token when the stop token comes
    right after a chunk."""
    run = _Run(FIRST_TOKENS + 4, last_flag=False)
    run.pieces()
    assert run.renders[-1] == (FIRST_TOKENS + 4 + SILENCE_TOKENS, FIRST_TOKENS * 10, True)


def test_nothing_to_say_renders_nothing() -> None:
    run = _Run(0)
    assert run.pieces() == []
    assert run.renders == []


def test_empty_renders_are_not_yielded() -> None:
    run = _Run(FIRST_TOKENS + 1)
    run.render = lambda t, done, final: (np.zeros(0, np.float32), done)  # type: ignore[method-assign]
    assert run.pieces() == []


# -- the speech tokenizer --------------------------------------------------------


def test_the_speech_tokenizer_download_is_refused_for_the_load_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    original = lambda repo_id, *a, **k: calls.append(repo_id)  # noqa: E731
    monkeypatch.setattr(huggingface_hub, "hf_hub_download", original)
    with chatterbox._without_speech_tokenizer():
        with pytest.raises(SpeechTokenizerSkipped):
            huggingface_hub.hf_hub_download(S3_TOKENIZER_REPO, "model.safetensors")
        huggingface_hub.hf_hub_download("someone/else", "x")
    assert huggingface_hub.hf_hub_download is original
    assert calls == ["someone/else"]
    with pytest.raises(RuntimeError), chatterbox._without_speech_tokenizer():
        raise RuntimeError("load failed")
    assert huggingface_hub.hf_hub_download is original


# -- load, without MLX ---------------------------------------------------------


@pytest.fixture
def fake_load(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[Any]:
    """``mlx_audio.tts.utils.load_model`` and ``_stream`` stand-ins; the
    models built, and snapshots that are folders under ``tmp_path``."""
    built: list[Any] = []

    def load_model(path: Path) -> Any:
        with pytest.raises(SpeechTokenizerSkipped):  # what the hook would meet
            huggingface_hub.hf_hub_download(S3_TOKENIZER_REPO, "model.safetensors")
        model = SimpleNamespace(
            path=path, sample_rate=TTS_SAMPLE_RATE, _conds=object(), _s3tokenizer=object()
        )
        built.append(model)
        return model

    def stream(model: Any, sentence: str) -> Iterator[NDArray[np.float32]]:
        model.said = [*getattr(model, "said", []), sentence]
        yield np.full(480, 0.5, dtype=np.float32)
        yield np.full(960, -0.25, dtype=np.float32)

    utils = SimpleNamespace(load_model=load_model)
    monkeypatch.setitem(sys.modules, "mlx_audio", SimpleNamespace(tts=SimpleNamespace()))
    monkeypatch.setitem(sys.modules, "mlx_audio.tts", SimpleNamespace(utils=utils))
    monkeypatch.setitem(sys.modules, "mlx_audio.tts.utils", utils)
    monkeypatch.setattr(chatterbox, "_stream", stream)
    monkeypatch.setattr(models, "snapshot", lambda repo, check_only=False: tmp_path / repo)
    return built


async def test_load_reads_the_pinned_snapshot_and_drops_the_speech_tokenizer(
    fake_load: list[Any], tmp_path: Path
) -> None:
    engine = ChatterboxEngine("mlx-community/chatterbox-turbo-fp16")
    await engine.load()
    (model,) = fake_load
    assert model.path == tmp_path / "mlx-community/chatterbox-turbo-fp16"
    assert model._s3tokenizer is None
    assert model.said == ["Warming up."]
    assert await engine.voices() == [DEFAULT_VOICE]


@pytest.mark.parametrize(
    "spoil, message",
    [
        (lambda m: setattr(m, "sample_rate", 44_100), "44100 Hz"),
        (lambda m: setattr(m, "_conds", None), "no default voice"),
    ],
)
async def test_a_load_that_fails_leaves_it_unloaded(
    fake_load: list[Any], monkeypatch: pytest.MonkeyPatch, spoil: Any, message: str
) -> None:
    utils = sys.modules["mlx_audio.tts.utils"]
    load = utils.load_model  # type: ignore[attr-defined]
    monkeypatch.setattr(utils, "load_model", lambda path: (m := load(path), spoil(m))[0])
    engine = ChatterboxEngine()
    with pytest.raises(RuntimeError, match=message):
        await engine.load()
    assert engine._model is None


async def test_each_piece_goes_out_as_16_bit_pcm(fake_load: list[Any]) -> None:
    engine = ChatterboxEngine()
    await engine.load()
    chunks = [c async for c in engine.synthesize("One. Two.", "default", speed=2.0)]
    pieces = [np.full(480, 0.5), np.full(960, -0.25)]
    expected = [np.rint(p * 32767).astype("<i2").tobytes() for p in pieces]
    assert [c.data for c in chunks] == expected * 2
    assert {c.sample_rate for c in chunks} == {TTS_SAMPLE_RATE}
    assert fake_load[0].said == ["Warming up.", "One.", "Two."]


async def test_an_unknown_voice_never_reaches_the_model(fake_load: list[Any]) -> None:
    engine = ChatterboxEngine()
    await engine.load()
    for voice in ("Default", "af_heart", "nobody"):
        with pytest.raises(UnknownVoice):
            await engine.synthesize("Hi.", voice).__anext__()
    assert fake_load[0].said == ["Warming up."]


# -- against the real model ----------------------------------------------------


@pytest.mark.model
def test_the_constants_match_mlx_audio() -> None:
    from mlx_audio.tts.models.chatterbox_turbo.models.s3gen import S3GEN_SIL

    assert SILENCE_TOKEN == S3GEN_SIL


@pytest.mark.model
def test_it_loads_the_default_voice_and_not_the_speech_tokenizer(chatterbox_engine: Any) -> None:
    assert chatterbox_engine._model._conds is not None
    assert chatterbox_engine._model._s3tokenizer is None


@pytest.mark.model
def test_synthesizes_plausible_duration(chatterbox_engine: Any) -> None:
    text = "The quick brown fox jumps over the lazy dog."

    async def collect() -> bytes:
        return b"".join([c.data async for c in chatterbox_engine.synthesize(text, "default")])

    seconds = len(asyncio.run(collect())) / 2 / TTS_SAMPLE_RATE
    assert 1.5 < seconds < 6.0, seconds


@pytest.mark.model
def test_streams_pieces_in_order_before_the_sentence_is_done(chatterbox_engine: Any) -> None:
    """Several pieces per sentence, the first one well before the last."""
    text = "This first sentence is long enough to be rendered in several pieces. Then another."

    async def timed() -> list[tuple[float, int]]:
        started = time.perf_counter()
        return [
            (time.perf_counter() - started, len(c.data))
            async for c in chatterbox_engine.synthesize(text, "default")
        ]

    arrivals = asyncio.run(timed())
    assert len(arrivals) >= 4
    times = [t for t, _ in arrivals]
    assert times == sorted(times)
    assert times[0] < 0.8, f"first audio after {times[0] * 1000:.0f} ms"
    assert times[0] < times[-1] / 3
    audio = sum(n for _, n in arrivals) / 2 / TTS_SAMPLE_RATE
    assert times[-1] < audio, "slower than real time"


@pytest.mark.model
def test_tags_survive_normalization_as_single_tokens(chatterbox_engine: Any) -> None:
    from voiced.text import normalize
    from voiced.text.sentences import split_sentences

    (sentence,) = split_sentences(normalize("[laugh] That is great news.", language="en"))
    tokenizer = chatterbox_engine._model.tokenizer
    assert tokenizer("[laugh]").input_ids == [tokenizer.convert_tokens_to_ids("[laugh]")]
    assert tokenizer(sentence).input_ids[0] == tokenizer.convert_tokens_to_ids("[laugh]")


@pytest.mark.model
def test_unknown_voice(chatterbox_engine: Any) -> None:
    async def first() -> None:
        await chatterbox_engine.synthesize("Hi.", "nobody").__anext__()

    with pytest.raises(UnknownVoice):
        asyncio.run(first())


@pytest.fixture
def tts_engine() -> ChatterboxEngine:
    """The engine the ``tts_client`` app loads (``tests/conftest.py``)."""
    return ChatterboxEngine()


@pytest.mark.model
async def test_voices_and_models_over_http(tts_client: AsyncClient) -> None:
    voices = (await tts_client.get("/v1/audio/voices")).json()["data"]
    assert [(v["id"], v["language"]) for v in voices] == [("default", "en")]
    (model,) = (await tts_client.get("/v1/models")).json()["data"]
    assert (model["id"], model["engine"], model["license"]) == (MODELS[0], "chatterbox", "MIT")
