"""Marvis TTS engine tests. The shared contract tests are in
``test_sentence_engines.py``; these cover what is Marvis's own, first with a
stand-in MLX (numpy) and mlx-audio (they run on CI), then against the real
runtime (``model``)."""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import sys
import threading
import time
import warnings
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import numpy as np
import pytest
from httpx import AsyncClient

from voiced import models
from voiced.engines import marvis
from voiced.engines.base import TTS_SAMPLE_RATE, UnknownVoice
from voiced.engines.catalogue import voice_label
from voiced.engines.marvis import (
    DEFAULT_VOICE,
    FRAME_SECONDS,
    MARVIS_VOICES,
    MIMI_FILE,
    MIMI_REPO,
    VOICES_REPO,
    MarvisEngine,
    max_frames,
)
from voiced.engines.registry import ModelRefused, get_tts_engine
from voiced.models.catalogue import MARVIS_TOKENIZER

FRAME = np.full(1920, 0.5, dtype=np.float32)
"""What the stand-in decodes for every frame: 80 ms."""
FRAMES = 2
"""Frames the stand-in samples before its all-zero end frame."""


def test_registered_with_its_model_and_licence() -> None:
    assert get_tts_engine("marvis") is MarvisEngine
    engine = MarvisEngine()
    assert (engine.model_id, engine.license) == (
        "Marvis-AI/marvis-tts-100m-v0.2-MLX-8bit",
        "Apache-2.0; codec: CC-BY-4.0",
    )
    assert MarvisEngine("Marvis-AI/marvis-tts-250m-v0.2-MLX-8bit").license == (
        "Apache-2.0; codec: CC-BY-4.0"
    )
    assert MarvisEngine("local/path").model_id == "local/path"


@pytest.mark.parametrize(
    "model", ["Marvis-AI/marvis-tts-250m-v0.2", "Marvis-AI/marvis-tts-250m-v0.2-MLX-4bit"]
)
def test_an_uncatalogued_marvis_model_is_refused(model: str) -> None:
    """The PyTorch checkpoint or another conversion would fail inside load()
    or go unbenchmarked; the message names the catalogued models."""
    with pytest.raises(ModelRefused) as exc:
        MarvisEngine(model)
    assert str(exc.value) == (
        f"{model} is not in voiced's model catalogue; its Marvis models are "
        "Marvis-AI/marvis-tts-100m-v0.2-MLX-8bit, Marvis-AI/marvis-tts-250m-v0.2-MLX-8bit"
    )


def test_the_module_imports_without_mlx() -> None:
    """The registry imports every engine module; MLX is imported in load()."""
    code = (
        "import sys; sys.modules['mlx'] = None; sys.modules['mlx_audio'] = None\n"
        "import voiced.engines.marvis"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


def test_the_four_published_voices_each_in_its_language() -> None:
    assert [(v.id, v.language) for v in MarvisEngine.catalogue_voices()] == [
        ("conversational_a", "en"),
        ("conversational_b", "en"),
        ("conversational_fr", "fr"),
        ("conversational_de", "de"),
    ]
    assert DEFAULT_VOICE.id == "conversational_a"
    assert MarvisEngine.languages == ["en", "fr", "de"]
    labels = [voice_label(v) for v in MARVIS_VOICES]
    assert labels == [
        "Conversational A (English, female)",
        "Conversational B (English, male)",
        "Conversational FR (French, male)",
        "Conversational DE (German, male)",
    ]


def test_the_catalogue_downloads_the_files_the_engine_loads() -> None:
    """The shared models' patterns in voiced.models are those files, no more."""
    voices, mimi = models.engine("marvis").shared  # type: ignore[union-attr]
    assert (voices.repo, voices.license) == (VOICES_REPO, "Apache-2.0")
    clips = [f"prompts/{v.id}.{ext}" for v in MARVIS_VOICES for ext in ("wav", "txt")]
    assert voices.patterns == (*MARVIS_TOKENIZER, *clips)
    assert (mimi.repo, mimi.license, mimi.patterns) == (MIMI_REPO, "CC-BY-4.0", (MIMI_FILE,))


def test_a_sentence_is_bounded_by_its_length() -> None:
    """0.12 s per character and 1 s more, in 80 ms frames."""
    assert max_frames("Hi.") == 17
    assert max_frames("x" * 100) == 163


class _Decoder:
    def __init__(self, model: _FakeModel) -> None:
        self.model = model

    def reset(self) -> None:
        self.model._here("reset decoder")

    def decode_frames(self, tokens: Any) -> Any:
        assert tokens.shape == (1, 32, 1)
        self.model._here(f"decode {int(tokens[0, 0, 0])}")
        return FRAME.reshape(1, 1, -1)


class _Backbone:
    """``model.model``: the transformers and their caches."""

    def __init__(self, model: _FakeModel) -> None:
        self.model = model
        self.frames = FRAMES
        """Frames before the end frame; None: never ends."""
        self.fail_at: int | None = None
        self.step = 0

    def reset_caches(self) -> None:
        self.step = 0
        self.model._here("reset caches")

    def generate_frame(self, tokens: Any, mask: Any, position: Any, sampler: Any) -> Any:
        self.step += 1
        self.model._here(f"frame {self.step}")
        self.model.inputs.append((tokens.shape, mask.shape, position.tolist()))
        if self.step == self.fail_at:
            raise RuntimeError("backbone failed")
        if self.frames is not None and self.step > self.frames:
            return np.zeros((1, 32), dtype=np.int32)
        return np.full((1, 32), self.step, dtype=np.int32)


class _FakeModel:
    """Stands in for mlx-audio's sesame ``Model``, recording what the engine
    calls and the thread of every call. It copies the private names the
    engine calls (``_tokenize_text_segment``, ``_tokenize_audio``,
    ``model.generate_frame``, ``_streaming_decoder``), so a test here cannot
    catch an upstream rename; the model tier does."""

    def __init__(self, config: dict[str, Any]) -> None:
        from mlx_audio.codec.models.mimi import mimi  # the stand-in

        self.config = config
        self.mimi = mimi.hf_hub_download(MIMI_REPO, MIMI_FILE)
        self.sample_rate = TTS_SAMPLE_RATE
        self.log: list[str] = []
        self.texts: list[str] = []
        self.inputs: list[tuple[Any, Any, Any]] = []
        self.weights: list[tuple[str, Any]] = []
        self.threads: set[int] = {threading.get_ident()}
        self.model = _Backbone(self)
        self._streaming_decoder = _Decoder(self)
        self._audio_tokenizer = SimpleNamespace(
            encoder=1, encoder_transformer=2, downsample=3, encoder_cache=[4], decoder=5
        )

    def _here(self, event: str | None = None) -> None:
        self.threads.add(threading.get_ident())
        if event:
            self.log.append(event)

    def sanitize(self, weights: dict[str, Any]) -> dict[str, Any]:
        return weights

    def model_quant_predicate(self, path: str, module: Any) -> bool:
        return True

    def load_weights(self, weights: list[tuple[str, Any]], strict: bool) -> None:
        self._here()
        self.weights = weights

    def parameters(self) -> dict[str, Any]:
        return {}

    def eval(self) -> None:
        self._here()

    def prepare_prompt(self, text: str, speaker: int, path: str, rate: int) -> Any:
        self._here(f"clip {Path(path).name}")
        assert (speaker, rate) == (0, TTS_SAMPLE_RATE)
        return SimpleNamespace(text=text, audio=np.zeros(rate))

    def _tokenize_audio(self, audio: Any, add_eos: bool) -> tuple[Any, Any]:
        assert not add_eos
        return np.ones((5, 33), dtype=np.int32), np.ones((5, 33), dtype=np.bool_)

    def _tokenize_text_segment(self, text: str, speaker: int) -> tuple[Any, Any]:
        self._here()
        self.texts.append(text)
        n = len(text.split())
        return np.zeros((n, 33), dtype=np.int32), np.zeros((n, 33), dtype=np.bool_)


def _fake_mx() -> SimpleNamespace:
    """numpy, under the names of ``mlx.core`` the engine uses."""
    return SimpleNamespace(
        concat=lambda arrays, axis: np.concatenate(arrays, axis=axis),
        arange=np.arange,
        zeros=np.zeros,
        ones_like=np.ones_like,
        all=lambda a: np.array(np.all(a)),
        int32=np.int32,
        bool_=np.bool_,
        eval=lambda *a: None,
        set_cache_limit=lambda n: None,
        clear_cache=lambda: None,
    )


@pytest.fixture
def fake_mlx(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[_FakeModel]:
    """MLX and mlx-audio modules whose models are recorded, snapshots that are
    folders under ``tmp_path``, and a GPU gate that logs each wait."""
    built: list[_FakeModel] = []
    core = _fake_mx()
    mlx = ModuleType("mlx")
    mlx.core = core  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mlx", mlx)
    monkeypatch.setitem(sys.modules, "mlx.core", core)

    def model(config: dict[str, Any]) -> _FakeModel:
        built.append(_FakeModel(config))
        return built[-1]

    def unpinned(repo_id: str, filename: str) -> str:
        raise AssertionError("the unpinned download was called")

    modules = {
        "mlx_audio.tts.models.sesame.sesame": SimpleNamespace(Model=model),
        "mlx_audio.utils": SimpleNamespace(
            load_weights=lambda root: {"file": Path(root).name},
            apply_quantization=lambda model, config, weights, predicate: None,
        ),
        "mlx_audio.lm.sample_utils": SimpleNamespace(make_sampler=lambda temp, top_k: None),
        "mlx_audio.codec.models.mimi.mimi": SimpleNamespace(hf_hub_download=unpinned),
    }
    for name, module in modules.items():
        parts = name.split(".")
        for i in range(1, len(parts)):
            monkeypatch.setitem(
                sys.modules,
                ".".join(parts[:i]),
                sys.modules.get(".".join(parts[:i])) or ModuleType(".".join(parts[:i])),
            )
        monkeypatch.setitem(sys.modules, name, module)
    # `from package import module` reads the attribute on the package.
    monkeypatch.setattr(
        sys.modules["mlx_audio.codec.models.mimi"],
        "mimi",
        modules["mlx_audio.codec.models.mimi.mimi"],
        raising=False,
    )

    for repo in (MarvisEngine.model_id, MIMI_REPO):
        (tmp_path / repo).mkdir(parents=True)
    (tmp_path / MarvisEngine.model_id / "config.json").write_text(
        json.dumps({"text_tokenizer": "Marvis-AI/marvis-tts-250m-v0.2"})
    )
    prompts = tmp_path / VOICES_REPO / "prompts"
    prompts.mkdir(parents=True)
    for v in MARVIS_VOICES:
        (prompts / f"{v.id}.txt").write_text(f"Clip {v.id}.\n")
    monkeypatch.setattr(models, "snapshot", lambda repo, check_only=False: tmp_path / repo)

    class Yielder:
        def __call__(self) -> None:
            built[-1].log.append("gpu")

    monkeypatch.setattr(marvis.gpu, "Yielder", Yielder)
    return built


async def _loaded(fake_mlx: list[_FakeModel]) -> tuple[MarvisEngine, _FakeModel]:
    engine = MarvisEngine()
    await engine.load()
    model = fake_mlx[-1]
    model.log.clear()  # the warm-up
    model.texts.clear()
    model.inputs.clear()
    return engine, model


def _sentence(frames: int = FRAMES) -> list[str]:
    """The log of one sentence that ends after ``frames``."""
    log = ["reset caches", "reset decoder"]
    for i in range(1, frames + 1):
        log += ["gpu", f"frame {i}", f"decode {i}"]
    return [*log, "gpu", f"frame {frames + 1}"]


async def test_load_reads_the_pinned_snapshots(fake_mlx: list[_FakeModel], tmp_path: Path) -> None:
    engine = MarvisEngine()
    await engine.load()
    (model,) = fake_mlx
    assert model.config["text_tokenizer"] == str(tmp_path / VOICES_REPO)
    assert model.mimi == str(tmp_path / MIMI_REPO / MIMI_FILE)
    assert dict(model.weights)["file"] == Path(MarvisEngine.model_id).name
    # Every clip encoded once, at load; then the warm-up.
    clips = [f"clip {v.id}.wav" for v in MARVIS_VOICES]
    assert model.log == clips + _sentence()
    assert model.texts == ["Clip conversational_a. Warming up."]
    assert await engine.voices() == MARVIS_VOICES
    # Mimi's encoder is freed once the voices are encoded; its decoder stays.
    assert vars(model._audio_tokenizer) == {"encoder_cache": [], "decoder": 5}
    # The unpinned download is back once the model is built.
    with pytest.raises(AssertionError, match="unpinned"):
        sys.modules["mlx_audio.codec.models.mimi.mimi"].hf_hub_download(MIMI_REPO, MIMI_FILE)


async def test_a_load_that_fails_leaves_it_unloaded(
    fake_mlx: list[_FakeModel], monkeypatch: pytest.MonkeyPatch
) -> None:
    def wrong_rate(config: dict[str, Any]) -> _FakeModel:
        model = _FakeModel(config)
        model.sample_rate = 44_100
        return model

    monkeypatch.setattr(sys.modules["mlx_audio.tts.models.sesame.sesame"], "Model", wrong_rate)
    engine = MarvisEngine()
    with pytest.raises(RuntimeError, match="44100 Hz"):
        await engine.load()
    assert engine._model is None


async def test_each_frame_goes_out_as_16_bit_pcm_after_the_gpu_gate(
    fake_mlx: list[_FakeModel],
) -> None:
    engine, model = await _loaded(fake_mlx)
    chunks = [c async for c in engine.synthesize("One. Two.", "conversational_fr")]
    expected = np.rint(FRAME * 32767).astype("<i2").tobytes()
    assert [c.data for c in chunks] == [expected] * (2 * FRAMES)
    assert {c.sample_rate for c in chunks} == {TTS_SAMPLE_RATE}
    assert model.log == _sentence() + _sentence()
    # The voice's transcript then the sentence, as one text.
    assert model.texts == ["Clip conversational_fr. One.", "Clip conversational_fr. Two."]


async def test_the_prompt_then_each_frame_with_its_position(fake_mlx: list[_FakeModel]) -> None:
    """The first step reads the text and the voice's five frames; each later
    step reads the frame before it, one position on."""
    engine, model = await _loaded(fake_mlx)
    [c async for c in engine.synthesize("One.", "conversational_a")]
    prompt = 3 + 5  # "Clip conversational_a. One." is three words, then the clip
    assert model.inputs == [
        ((1, prompt, 33), (1, prompt, 33), [list(range(prompt))]),
        ((1, 1, 33), (1, 1, 33), [[prompt]]),
        ((1, 1, 33), (1, 1, 33), [[prompt + 1]]),
    ]


async def test_a_runaway_sentence_stops_at_its_bound(
    fake_mlx: list[_FakeModel], caplog: pytest.LogCaptureFixture
) -> None:
    """The stand-in never samples the end frame; it fails at its 10,000th
    frame, so a missing bound raises instead of hanging the test. The last
    frame before the bound fades out over 10 ms."""
    engine, model = await _loaded(fake_mlx)
    model.model.frames = None
    model.model.fail_at = 10_000
    with caplog.at_level(logging.WARNING, logger="voiced.engines.marvis"):
        chunks = [c async for c in engine.synthesize("Hi.", "conversational_a")]
    assert len(chunks) == max_frames("Hi.")
    assert "runaway generation for 'Hi.': cut at the 17-frame length bound" in caplog.text
    full = np.rint(FRAME * 32767).astype("<i2").tobytes()
    assert {c.data for c in chunks[:-1]} == {full}
    last = np.frombuffer(chunks[-1].data, dtype="<i2")
    assert (last[:-240] == last[0]).all() and last[-1] == 0
    assert np.all(np.diff(last[-240:]) <= 0)


async def test_the_load_and_every_step_run_on_one_thread(fake_mlx: list[_FakeModel]) -> None:
    """MLX arrays a sentence keeps between frames cannot be evaluated on
    another thread. The model is built on the thread too, so a load that ran
    elsewhere fails this."""
    engine = MarvisEngine()
    await engine.load()
    (model,) = fake_mlx
    for _ in range(3):
        [c async for c in engine.synthesize("One. Two.", "conversational_a")]
    gen = engine.synthesize("One. Two.", "conversational_a")
    await gen.__anext__()
    await gen.aclose()
    assert len(model.threads) == 1
    assert threading.get_ident() not in model.threads


async def test_a_client_leaving_mid_sentence_stops_the_sentence(
    fake_mlx: list[_FakeModel],
) -> None:
    engine, model = await _loaded(fake_mlx)
    gen = engine.synthesize("One. Two.", "conversational_a", speed=2.0)  # speed is ignored
    await gen.__anext__()
    await gen.aclose()
    assert model.log == ["reset caches", "reset decoder", "gpu", "frame 1", "decode 1"]
    assert len([c async for c in engine.synthesize("Three.", "conversational_a")]) == FRAMES


async def test_a_failing_frame_raises_and_frees_the_model(fake_mlx: list[_FakeModel]) -> None:
    engine, model = await _loaded(fake_mlx)
    model.model.fail_at = 2
    with pytest.raises(RuntimeError, match="backbone failed"):
        [c async for c in engine.synthesize("One. Two.", "conversational_a")]
    model.model.fail_at = None
    assert len([c async for c in engine.synthesize("One.", "conversational_a")]) == FRAMES


async def test_a_second_sentence_at_once_is_refused_not_garbled(
    fake_mlx: list[_FakeModel],
) -> None:
    """The caches and Mimi's decoder state are the model's; the exclusive
    slot keeps sentences apart, and the engine refuses an overlap if it ever
    happens."""
    engine, _ = await _loaded(fake_mlx)
    first = engine._synth_chunks("One.", "conversational_a", 1.0)
    next(first)
    with pytest.raises(RuntimeError, match="one sentence at a time"):
        next(engine._synth_chunks("Two.", "conversational_a", 1.0))
    first.close()
    assert len(list(engine._synth_chunks("Three.", "conversational_a", 1.0))) == FRAMES


async def test_a_sentence_without_letters_or_digits_is_not_spoken(
    fake_mlx: list[_FakeModel],
) -> None:
    engine, model = await _loaded(fake_mlx)
    for text in ("...", "!!!", "?"):
        assert [c async for c in engine.synthesize(text, "conversational_a")] == []
    assert model.log == []


async def test_an_unknown_voice_never_reaches_the_model(fake_mlx: list[_FakeModel]) -> None:
    engine, model = await _loaded(fake_mlx)
    for voice in ("conversational_es", "Conversational_A", "nobody"):
        with pytest.raises(UnknownVoice):
            await engine.synthesize("Hi.", voice).__anext__()
    assert model.log == []


# -- against the real runtime ------------------------------------------------

FRAME_BYTES = 1920 * 2
"""One 80 ms frame of 16-bit PCM."""

SAMPLES = {
    "en": "The quick brown fox jumps over the lazy dog.",
    "fr": "Le renard brun rapide saute par-dessus le chien paresseux.",
    "de": "Der schnelle braune Fuchs springt über den faulen Hund.",
}


@pytest.mark.model
def test_the_voices_and_mimi_are_in_the_pinned_snapshots() -> None:
    root = models.snapshot(VOICES_REPO, check_only=True)
    for path in MARVIS_TOKENIZER:
        assert (root / path).is_file(), path
    for v in MARVIS_VOICES:
        assert (root / "prompts" / f"{v.id}.wav").is_file(), v.id
    assert (models.snapshot(MIMI_REPO, check_only=True) / MIMI_FILE).is_file()


@pytest.mark.model
@pytest.mark.parametrize("voice", MARVIS_VOICES, ids=lambda v: v.id)
def test_synthesizes_plausible_duration(marvis_engine: Any, voice: Any) -> None:
    """Within the length bound always; a sentence that reaches it (the model
    did not end it, which happens now and then, ADR 0031) is reported as a
    warning, not a failure."""
    text = SAMPLES[voice.language]

    async def collect() -> bytes:
        return b"".join([c.data async for c in marvis_engine.synthesize(text, voice.id)])

    frames = len(asyncio.run(collect())) // FRAME_BYTES
    assert 1.5 < frames * FRAME_SECONDS, frames
    assert frames <= max_frames(text), frames
    if frames == max_frames(text):
        warnings.warn(f"{voice.id} ran on to the {frames}-frame bound", stacklevel=1)


@pytest.mark.model
def test_streams_frames_in_order_before_the_sentence_is_done(marvis_engine: Any) -> None:
    """One 80 ms frame per chunk, the first long before the last."""
    first = "This first sentence is long enough to be decoded in many frames."

    async def timed() -> list[tuple[float, int]]:
        started = time.perf_counter()
        return [
            (time.perf_counter() - started, len(c.data))
            async for c in marvis_engine.synthesize(
                f"{first} Then a second one.", "conversational_a"
            )
        ]

    asyncio.run(timed())  # warm
    arrivals = asyncio.run(timed())
    assert len(arrivals) >= 20
    assert {n for _, n in arrivals} == {1920 * 2}
    times = [t for t, _ in arrivals]
    assert times == sorted(times)
    assert times[0] < 0.3, f"first audio after {times[0] * 1000:.0f} ms"
    assert times[0] < times[-1] / 4


@pytest.mark.model
def test_unknown_voice(marvis_engine: Any) -> None:
    async def first() -> None:
        await marvis_engine.synthesize("Hi.", "conversational_es").__anext__()

    with pytest.raises(UnknownVoice):
        asyncio.run(first())


@pytest.fixture
def tts_engine() -> MarvisEngine:
    """The engine the ``tts_client`` app loads (``tests/conftest.py``)."""
    return MarvisEngine()


@pytest.mark.model
async def test_voices_and_models_over_http(tts_client: AsyncClient) -> None:
    voices = (await tts_client.get("/v1/audio/voices")).json()["data"]
    # The test config serves English only.
    assert [v["id"] for v in voices] == ["conversational_a", "conversational_b"]
    b = next(v for v in voices if v["id"] == "conversational_b")
    assert (b["language"], b["gender"]) == ("en", "male")
    (model,) = (await tts_client.get("/v1/models")).json()["data"]
    assert (model["id"], model["engine"], model["license"]) == (
        "Marvis-AI/marvis-tts-100m-v0.2-MLX-8bit",
        "marvis",
        "Apache-2.0; codec: CC-BY-4.0",
    )
