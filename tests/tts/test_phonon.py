"""Phonon engine tests. The shared contract tests are in
``test_sentence_engines.py``; these cover what is Phonon's own, first with a
stand-in ``ptts`` module (they run on CI), then against the real runtime
(``model``)."""

from __future__ import annotations

import asyncio
import json
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

from voiced import models
from voiced.engines.base import TTS_SAMPLE_RATE, UnknownVoice, Voice
from voiced.engines.catalogue import voice_label
from voiced.engines.phonon import (
    DEFAULT_VOICE,
    PHONON_VOICES,
    VOICE_FILES,
    VOICES_REPO,
    PhononEngine,
)
from voiced.engines.registry import create_tts_engine, get_tts_engine

PIECES = [np.full(1920, 0.5, dtype=np.float32), np.full(3840, -0.25, dtype=np.float32)]
"""What the stand-in decodes for every sentence: two pieces, 80 and 160 ms."""


def test_registered_with_its_model_and_licence() -> None:
    assert get_tts_engine("phonon") is PhononEngine
    assert PhononEngine().license == "Apache-2.0"
    assert PhononEngine("local/path").model_id == "local/path"
    # The licence of the configured model, from the catalogue (the registry sets it).
    assert create_tts_engine("phonon", "gradium/phonon-40m-en").license == "CC-BY-NC-4.0"
    assert create_tts_engine("phonon", "gradium/phonon-31m-en").license == "Apache-2.0"
    assert PhononEngine.license == "Apache-2.0"  # the class default is untouched


@pytest.mark.parametrize("variant", ["fr", "de", "es", "pt", "multilingual"])
def test_the_other_40m_models_are_refused(variant: str) -> None:
    """Only the catalogued 40M model loads; the message names the model."""
    model = f"gradium/phonon-40m-{variant}"
    with pytest.raises(ValueError) as exc:
        PhononEngine(model)
    assert str(exc.value) == (
        f"{model} is not in voiced's model catalogue; of the Phonon 40M models it lists "
        "gradium/phonon-40m-en"
    )


def test_the_catalogue_downloads_the_voices_the_engine_loads() -> None:
    """The voices' patterns in voiced.models are the presets' files, no more."""
    (voices,) = models.engine("phonon").shared  # type: ignore[union-attr]
    assert voices.repo == VOICES_REPO
    assert voices.patterns == ("voices.jsonl", *VOICE_FILES.values())


def test_the_module_imports_without_ptts() -> None:
    """The registry imports every engine module; ptts is imported in load()."""
    code = "import sys; sys.modules['ptts'] = None; import voiced.engines.phonon"
    subprocess.run([sys.executable, "-c", code], check=True)


def test_the_catalogue_is_the_default_voice_then_the_english_presets() -> None:
    assert PhononEngine.catalogue_voices()[0] == DEFAULT_VOICE
    ids = [v.id for v in PHONON_VOICES]
    assert len(ids) == len(set(ids))
    assert set(ids[1:]) == set(VOICE_FILES)
    by_id = {v.id: v for v in PHONON_VOICES}
    assert voice_label(by_id["Easton"]) == "Easton (American English, male)"
    assert voice_label(by_id["Maeve"]) == "Maeve (British English, female)"
    assert voice_label(by_id["The Snow Queen"]) == "The Snow Queen"


class _Stream:
    def __init__(self, log: list[str]) -> None:
        self.log = log

    def __enter__(self) -> _Stream:
        return self

    def __exit__(self, *exc: object) -> bool:
        self.log.append("closed")
        return False

    def __iter__(self) -> Iterator[np.ndarray]:
        for piece in PIECES:
            self.log.append("piece")
            yield piece


class _FakeTTS:
    """Stands in for ``ptts.TTS``."""

    def __init__(self, model: str, **kwargs: Any) -> None:
        self.model = model
        self.kwargs = kwargs
        self.sample_rate = TTS_SAMPLE_RATE
        self.quant, self.device = "q8_0", "cpu"
        self.voices = ["default"]
        self.added: dict[str, str] = {}
        self.log: list[str] = []
        self.threads: int | None = None

    def add_voice(self, name: str, path: str) -> None:
        self.added[name] = path
        self.voices.append(name)

    def stream(self, text: str, *, voice: str) -> _Stream:
        self.log.append(f"stream {text} {voice}")
        return _Stream(self.log)


@pytest.fixture
def fake_ptts(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[_FakeTTS]:
    """A ``ptts`` module whose models are recorded, and snapshots that are
    folders under ``tmp_path``."""
    built: list[_FakeTTS] = []
    module = SimpleNamespace(threads=None)

    def tts(model: str, **kwargs: Any) -> _FakeTTS:
        built.append(_FakeTTS(model, **kwargs))
        built[-1].threads = module.threads  # the pool it was built with
        return built[-1]

    module.TTS = tts
    module.set_num_threads = lambda n: setattr(module, "threads", n)
    monkeypatch.setitem(sys.modules, "ptts", module)
    monkeypatch.setattr(models, "snapshot", lambda repo, check_only=False: tmp_path / repo)
    return built


async def _loaded(fake_ptts: list[_FakeTTS]) -> tuple[PhononEngine, _FakeTTS]:
    engine = PhononEngine()
    await engine.load()
    model = fake_ptts[-1]
    model.log.clear()  # the warm-up
    return engine, model


async def test_load_reads_the_pinned_snapshots_on_the_cpu(
    fake_ptts: list[_FakeTTS], tmp_path: Path
) -> None:
    engine = PhononEngine()
    await engine.load()
    (model,) = fake_ptts
    assert model.model == str(tmp_path / PhononEngine.model_id)
    voices = tmp_path / VOICES_REPO
    assert model.kwargs == {
        "device": "cpu",
        "quant": "q8",
        "lang": "en",
        "voice_catalog": str(voices),
    }
    assert model.threads == 4  # set before the model, not ptts's every core
    assert model.added == {name: str(voices / path) for name, path in VOICE_FILES.items()}
    assert model.log == ["stream Warming up. default", "piece", "piece", "closed"]
    assert await engine.voices() == PHONON_VOICES


@pytest.mark.parametrize(
    "spoil, message",
    [
        (lambda m: setattr(m, "sample_rate", 44_100), "44100 Hz"),
        (lambda m: setattr(m, "voices", []), "no default voice"),
    ],
)
async def test_a_load_that_fails_leaves_it_unloaded(
    fake_ptts: list[_FakeTTS], monkeypatch: pytest.MonkeyPatch, spoil: Any, message: str
) -> None:
    def spoilt(model: str, **kwargs: Any) -> _FakeTTS:
        tts = _FakeTTS(model, **kwargs)
        spoil(tts)
        return tts

    monkeypatch.setattr(sys.modules["ptts"], "TTS", spoilt)
    engine = PhononEngine()
    with pytest.raises(RuntimeError, match=message):
        await engine.load()
    assert engine._model is None


async def test_each_piece_goes_out_as_16_bit_pcm(fake_ptts: list[_FakeTTS]) -> None:
    engine, model = await _loaded(fake_ptts)
    chunks = [c async for c in engine.synthesize("One. Two.", "Easton")]
    expected = [np.rint(np.clip(p, -1, 1) * 32767).astype("<i2").tobytes() for p in PIECES]
    assert [c.data for c in chunks] == expected * 2
    assert {c.sample_rate for c in chunks} == {TTS_SAMPLE_RATE}
    sentence = ["piece", "piece", "closed"]
    assert model.log == ["stream One. Easton", *sentence, "stream Two. Easton", *sentence]


async def test_a_client_leaving_mid_sentence_stops_phonon(fake_ptts: list[_FakeTTS]) -> None:
    engine, model = await _loaded(fake_ptts)
    gen = engine.synthesize("One. Two.", "default", speed=2.0)  # speed is ignored
    await gen.__anext__()
    await gen.aclose()
    assert model.log == ["stream One. default", "piece", "closed"]


async def test_a_sentence_without_letters_or_digits_is_not_spoken(
    fake_ptts: list[_FakeTTS],
) -> None:
    """Phonon reads "..." or "!!!" as a second or more of sound."""
    engine, model = await _loaded(fake_ptts)
    for text in ("...", "!!!", "?"):
        assert [c async for c in engine.synthesize(text, "default")] == []
    assert model.log == []
    chunks = [c async for c in engine.synthesize("Wait... 42!", "default")]  # one sentence
    assert len(chunks) == len(PIECES)


async def test_an_unknown_voice_never_reaches_phonon(fake_ptts: list[_FakeTTS]) -> None:
    engine, model = await _loaded(fake_ptts)
    # Nele is in Gradium's catalogue, in German; voiced serves the English presets.
    for voice in ("Nele", "easton", "nobody"):
        with pytest.raises(UnknownVoice):
            await engine.synthesize("Hi.", voice).__anext__()
    assert model.log == []


# -- against the real runtime ------------------------------------------------


@pytest.mark.model
def test_the_voice_table_is_the_pinned_catalogue() -> None:
    """The English rows of voices.jsonl at the pinned revision."""
    path = models.snapshot(VOICES_REPO) / "voices.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    english = [r for r in rows if r["language"] == "en"]
    assert {r["name"]: r["filename"] for r in english} == VOICE_FILES
    for row, voice in zip(english, PHONON_VOICES[1:], strict=True):
        locale = row["locale"]
        tag = "en" if locale == "characters" else f"en-{locale.split('-')[1].upper()}"
        assert voice == Voice(id=row["name"], name=row["name"], language=tag, gender=row["gender"])


@pytest.mark.model
def test_it_loads_every_voice(phonon_engine: Any) -> None:
    assert set(phonon_engine._model.voices) == {v.id for v in PHONON_VOICES}
    assert phonon_engine._model.device == "cpu"


@pytest.mark.model
@pytest.mark.parametrize("voice", ["default", "Easton", "Maeve", "Grub the Ogre"])
def test_synthesizes_plausible_duration(phonon_engine: Any, voice: str) -> None:
    text = "The quick brown fox jumps over the lazy dog."

    async def collect() -> bytes:
        return b"".join([c.data async for c in phonon_engine.synthesize(text, voice)])

    seconds = len(asyncio.run(collect())) / 2 / TTS_SAMPLE_RATE
    assert 1.0 < seconds < 6.0, seconds


@pytest.mark.model
def test_streams_pieces_in_order_before_the_sentence_is_done(phonon_engine: Any) -> None:
    """Several pieces per sentence, the first one long before the last."""
    first = "This first sentence is long enough to be decoded in several pieces."

    async def timed() -> list[tuple[float, int]]:
        started = time.perf_counter()
        return [
            (time.perf_counter() - started, len(c.data))
            async for c in phonon_engine.synthesize(f"{first} Then a second one.", "Easton")
        ]

    asyncio.run(timed())  # warm
    arrivals = asyncio.run(timed())
    assert len(arrivals) >= 10
    times = [t for t, _ in arrivals]
    assert times == sorted(times)
    assert times[0] < 0.1, f"first audio after {times[0] * 1000:.0f} ms"
    assert times[0] < times[1] < times[-1]
    assert times[0] < times[-1] / 4


@pytest.mark.model
def test_unknown_voice(phonon_engine: Any) -> None:
    async def first() -> None:
        await phonon_engine.synthesize("Hi.", "nobody").__anext__()

    with pytest.raises(UnknownVoice):
        asyncio.run(first())


@pytest.fixture
def tts_engine() -> PhononEngine:
    """The engine the ``tts_client`` app loads (``tests/conftest.py``)."""
    return PhononEngine()


@pytest.mark.model
async def test_voices_and_models_over_http(tts_client: AsyncClient) -> None:
    voices = (await tts_client.get("/v1/audio/voices")).json()["data"]
    assert [v["id"] for v in voices] == [v.id for v in PHONON_VOICES]
    easton = next(v for v in voices if v["id"] == "Easton")
    assert (easton["language"], easton["gender"]) == ("en-US", "male")
    (model,) = (await tts_client.get("/v1/models")).json()["data"]
    assert (model["id"], model["engine"], model["license"]) == (
        "gradium/phonon-31m-en",
        "phonon",
        "Apache-2.0",
    )


@pytest.mark.model
def test_the_40m_model_with_a_token() -> None:
    """Gated: needs its licence accepted and a token where huggingface_hub
    reads it."""
    from huggingface_hub import get_token

    if get_token() is None:
        pytest.skip("no Hugging Face token")
    engine = create_tts_engine("phonon", "gradium/phonon-40m-en")  # the licence of its model

    async def speak() -> bytes:
        await engine.load()
        return b"".join([c.data async for c in engine.synthesize("Hello there.", "Easton")])

    assert len(asyncio.run(speak())) / 2 / TTS_SAMPLE_RATE > 0.3
    assert engine.license == "CC-BY-NC-4.0"
