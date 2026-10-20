"""Pocket TTS engine tests. The shared contract tests are in
``test_sentence_engines.py``; these cover what is Pocket's own, first with a
stand-in MLX and mlx-audio (they run on CI), then against the real runtime
(``model``)."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import numpy as np
import pytest
from httpx import AsyncClient

from voiced import models
from voiced.engines import pocket
from voiced.engines.base import TTS_SAMPLE_RATE, UnknownVoice
from voiced.engines.catalogue import voice_label
from voiced.engines.pocket import (
    DEFAULT_VOICE,
    POCKET_VOICES,
    TOKENIZER,
    VOICE_FILES,
    VOICES_REPO,
    PocketEngine,
)
from voiced.engines.registry import get_tts_engine

FRAMES = [np.full(1920, 0.5, dtype=np.float32), np.full(1920, -0.25, dtype=np.float32)]
"""What the stand-in decodes for every sentence: two 80 ms frames."""


def test_registered_with_its_model_and_licence() -> None:
    assert get_tts_engine("pocket") is PocketEngine
    engine = PocketEngine()
    assert (engine.model_id, engine.license) == ("mlx-community/pocket-tts", "CC-BY-4.0")
    assert PocketEngine("local/path").model_id == "local/path"


def test_the_module_imports_without_mlx() -> None:
    """The registry imports every engine module; MLX is imported in load()."""
    code = (
        "import sys; sys.modules['mlx'] = None; sys.modules['mlx_audio'] = None\n"
        "import voiced.engines.pocket"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


def test_only_the_voices_licensed_for_commercial_use() -> None:
    """cosette (Expresso) and jean (EARS) are CC-BY-NC."""
    ids = [v.id for v in PocketEngine.catalogue_voices()]
    assert ids == ["alba", "marius", "javert", "fantine", "eponine", "azelma"]
    assert DEFAULT_VOICE.id == "alba"
    assert not {"cosette", "jean"} & set(VOICE_FILES)
    assert voice_label(POCKET_VOICES[0]) == "Alba (English, female)"


def test_the_catalogue_downloads_the_tokenizer_and_the_voices_the_engine_loads() -> None:
    """The shared model's patterns in voiced.models are those files, no more."""
    (voices,) = models.engine("pocket").shared  # type: ignore[union-attr]
    assert (voices.repo, voices.license) == (VOICES_REPO, "CC-BY-4.0")
    assert voices.patterns == (TOKENIZER, *VOICE_FILES.values())


class _FakeModel:
    """Stands in for mlx-audio's Pocket TTS ``Model``."""

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.sample_rate = TTS_SAMPLE_RATE
        self.weights: list[tuple[str, Any]] = []
        self.log: list[str] = []
        self.threads: set[int] = {threading.get_ident()}

    def load_weights(self, weights: list[tuple[str, Any]]) -> None:
        self.weights = weights

    def parameters(self) -> dict[str, Any]:
        return {}

    def eval(self) -> None:
        pass

    def init_state(self) -> dict[str, Any]:
        return {}

    def _run_flow_lm_and_increment_step(
        self, model_state: dict[str, Any], audio_conditioning: Any
    ) -> None:
        model_state["voice"] = audio_conditioning.name

    def _slice_flow_cache(self, model_state: dict[str, Any], num_frames: int) -> None:
        model_state["frames"] = num_frames

    def generate_audio_stream(self, model_state: dict[str, Any], text: str) -> Iterator[Any]:
        self.log.append(f"stream {text} {model_state['voice']} {model_state['frames']}")
        try:
            for frame in FRAMES:
                self.threads.add(threading.get_ident())
                self.log.append("frame")
                yield frame
        finally:
            self.threads.add(threading.get_ident())
            self.log.append("closed")


@pytest.fixture
def fake_mlx(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[_FakeModel]:
    """MLX and mlx-audio modules whose models are recorded, snapshots that are
    folders under ``tmp_path``, and a GPU gate that logs each wait."""
    built: list[_FakeModel] = []

    def load(path: str) -> dict[str, Any]:
        name = Path(path).stem
        return {"audio_prompt": SimpleNamespace(name=name, shape=(1, 125, 1024)), "file": name}

    core = SimpleNamespace(
        load=load, eval=lambda *a: None, set_cache_limit=lambda n: None, clear_cache=lambda: None
    )
    mlx = ModuleType("mlx")
    mlx.core = core  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mlx", mlx)
    monkeypatch.setitem(sys.modules, "mlx.core", core)

    def model(config: dict[str, Any]) -> _FakeModel:
        built.append(_FakeModel(config))
        return built[-1]

    name = "mlx_audio.tts.models.pocket_tts.pocket_tts"
    parts = name.split(".")
    for i in range(1, len(parts)):
        monkeypatch.setitem(sys.modules, ".".join(parts[:i]), ModuleType(".".join(parts[:i])))
    monkeypatch.setitem(sys.modules, name, SimpleNamespace(Model=model))

    root = tmp_path / PocketEngine.model_id
    root.mkdir(parents=True)
    config = {"flow_lm": {"lookup_table": {"tokenizer_path": "hf://somewhere/tokenizer.model"}}}
    (root / "config.json").write_text(json.dumps(config))
    monkeypatch.setattr(models, "snapshot", lambda repo, check_only=False: tmp_path / repo)

    class Yielder:
        def __call__(self) -> None:
            built[-1].log.append("gpu")

    monkeypatch.setattr(pocket.gpu, "Yielder", Yielder)
    return built


async def _loaded(fake_mlx: list[_FakeModel]) -> tuple[PocketEngine, _FakeModel]:
    engine = PocketEngine()
    await engine.load()
    model = fake_mlx[-1]
    model.log.clear()  # the warm-up
    return engine, model


async def test_load_reads_the_pinned_snapshots(fake_mlx: list[_FakeModel], tmp_path: Path) -> None:
    engine = PocketEngine()
    await engine.load()
    (model,) = fake_mlx
    voices = tmp_path / VOICES_REPO
    assert model.config["flow_lm"]["lookup_table"]["tokenizer_path"] == str(voices / TOKENIZER)
    assert dict(model.weights)["file"] == "model"
    assert {name: p.name for name, p in engine._prompts.items()} == {n: n for n in VOICE_FILES}
    frames = ["gpu", "stream Warming up. alba 125", "frame", "gpu", "frame", "gpu", "closed"]
    assert model.log == frames
    assert await engine.voices() == POCKET_VOICES


async def test_a_load_that_fails_leaves_it_unloaded(
    fake_mlx: list[_FakeModel], monkeypatch: pytest.MonkeyPatch
) -> None:
    def wrong_rate(config: dict[str, Any]) -> _FakeModel:
        model = _FakeModel(config)
        model.sample_rate = 44_100
        return model

    monkeypatch.setattr(
        sys.modules["mlx_audio.tts.models.pocket_tts.pocket_tts"], "Model", wrong_rate
    )
    engine = PocketEngine()
    with pytest.raises(RuntimeError, match="44100 Hz"):
        await engine.load()
    assert engine._model is None


async def test_each_frame_goes_out_as_16_bit_pcm_after_the_gpu_gate(
    fake_mlx: list[_FakeModel],
) -> None:
    engine, model = await _loaded(fake_mlx)
    chunks = [c async for c in engine.synthesize("One. Two.", "javert")]
    expected = [np.rint(np.clip(f, -1, 1) * 32767).astype("<i2").tobytes() for f in FRAMES]
    assert [c.data for c in chunks] == expected * 2
    assert {c.sample_rate for c in chunks} == {TTS_SAMPLE_RATE}

    def sentence(text: str) -> list[str]:
        return ["gpu", f"stream {text} javert 125", "frame", "gpu", "frame", "gpu", "closed"]

    assert model.log == sentence("One.") + sentence("Two.")


async def test_every_step_runs_on_one_model_thread(fake_mlx: list[_FakeModel]) -> None:
    """MLX arrays a sentence keeps between frames cannot be evaluated on
    another thread."""
    engine, model = await _loaded(fake_mlx)
    model.threads.clear()
    for _ in range(3):
        [c async for c in engine.synthesize("One. Two.", "alba")]
    gen = engine.synthesize("One. Two.", "alba")
    await gen.__anext__()
    await gen.aclose()
    assert len(model.threads) == 1
    assert threading.get_ident() not in model.threads


async def test_a_client_leaving_mid_sentence_stops_the_sentence(
    fake_mlx: list[_FakeModel],
) -> None:
    engine, model = await _loaded(fake_mlx)
    gen = engine.synthesize("One. Two.", "alba", speed=2.0)  # speed is ignored
    await gen.__anext__()
    await gen.aclose()
    assert model.log == ["gpu", "stream One. alba 125", "frame", "closed"]


async def test_a_sentence_without_letters_or_digits_is_not_spoken(
    fake_mlx: list[_FakeModel],
) -> None:
    engine, model = await _loaded(fake_mlx)
    for text in ("...", "!!!", "?"):
        assert [c async for c in engine.synthesize(text, "alba")] == []
    assert model.log == []


async def test_an_unknown_or_non_commercial_voice_never_reaches_the_model(
    fake_mlx: list[_FakeModel],
) -> None:
    engine, model = await _loaded(fake_mlx)
    for voice in ("cosette", "jean", "Alba", "nobody"):
        with pytest.raises(UnknownVoice):
            await engine.synthesize("Hi.", voice).__anext__()
    assert model.log == []


# -- against the real runtime ------------------------------------------------


@pytest.mark.model
def test_the_voices_are_in_the_pinned_snapshot() -> None:
    root = models.snapshot(VOICES_REPO, check_only=True)
    for path in (TOKENIZER, *VOICE_FILES.values()):
        assert (root / path).is_file(), path


@pytest.mark.model
@pytest.mark.parametrize("voice", [v.id for v in POCKET_VOICES])
def test_synthesizes_plausible_duration(pocket_engine: Any, voice: str) -> None:
    text = "The quick brown fox jumps over the lazy dog."

    async def collect() -> bytes:
        return b"".join([c.data async for c in pocket_engine.synthesize(text, voice)])

    seconds = len(asyncio.run(collect())) / 2 / TTS_SAMPLE_RATE
    assert 1.0 < seconds < 6.0, seconds


@pytest.mark.model
def test_streams_frames_in_order_before_the_sentence_is_done(pocket_engine: Any) -> None:
    """One 80 ms frame per chunk, the first long before the last."""
    first = "This first sentence is long enough to be decoded in many frames."

    async def timed() -> list[tuple[float, int]]:
        started = time.perf_counter()
        return [
            (time.perf_counter() - started, len(c.data))
            async for c in pocket_engine.synthesize(f"{first} Then a second one.", "alba")
        ]

    asyncio.run(timed())  # warm
    arrivals = asyncio.run(timed())
    assert len(arrivals) >= 20
    assert {n for _, n in arrivals} == {1920 * 2}
    times = [t for t, _ in arrivals]
    assert times == sorted(times)
    assert times[0] < 0.15, f"first audio after {times[0] * 1000:.0f} ms"
    assert times[0] < times[-1] / 4


@pytest.mark.model
def test_unknown_voice(pocket_engine: Any) -> None:
    async def first() -> None:
        await pocket_engine.synthesize("Hi.", "cosette").__anext__()

    with pytest.raises(UnknownVoice):
        asyncio.run(first())


@pytest.fixture
def tts_engine() -> PocketEngine:
    """The engine the ``tts_client`` app loads (``tests/conftest.py``)."""
    return PocketEngine()


@pytest.mark.model
async def test_voices_and_models_over_http(tts_client: AsyncClient) -> None:
    voices = (await tts_client.get("/v1/audio/voices")).json()["data"]
    assert [v["id"] for v in voices] == [v.id for v in POCKET_VOICES]
    javert = next(v for v in voices if v["id"] == "javert")
    assert (javert["language"], javert["gender"]) == ("en", "male")
    (model,) = (await tts_client.get("/v1/models")).json()["data"]
    assert (model["id"], model["engine"], model["license"]) == (
        "mlx-community/pocket-tts",
        "pocket",
        "CC-BY-4.0",
    )
