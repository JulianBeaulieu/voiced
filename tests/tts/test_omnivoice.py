"""OmniVoice engine tests. The shared contract tests are in
``test_sentence_engines.py``; these cover what is OmniVoice's own, first
without MLX (they run on CI), then against the real model (``model``)."""

from __future__ import annotations

import asyncio
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest
from httpx import AsyncClient

from voiced import gpu, models
from voiced.config import Settings
from voiced.engines import omnivoice
from voiced.engines.base import TTS_SAMPLE_RATE, UnknownVoice
from voiced.engines.catalogue import voice_label
from voiced.engines.omnivoice import (
    DESIGNED,
    LANGUAGES,
    OMNIVOICE_VOICES,
    OmniVoiceEngine,
    frames_to_fill,
    tidy,
)
from voiced.engines.registry import ModelRefused, create_tts_engine, get_tts_engine

RATE = TTS_SAMPLE_RATE
SIX = ["en", "de", "apc", "es", "fr", "zh"]


def test_registered_with_its_model_and_both_licences() -> None:
    assert get_tts_engine("omnivoice") is OmniVoiceEngine
    engine = OmniVoiceEngine()
    assert engine.model_id == "k2-fsa/OmniVoice"
    assert "CC-BY-NC" in engine.license and "Higgs Audio 2" in engine.license
    assert models.pinned_revision("k2-fsa/OmniVoice") is not None
    assert "omnivoice" not in models.DEFAULT_ENGINES.values()


def test_only_the_catalogued_model_loads() -> None:
    """Not the mlx-community conversions, whose licence tag is wrong."""
    for model in ("mlx-community/OmniVoice-bf16", "someone/else"):
        with pytest.raises(ModelRefused, match=f"{model} is not in voiced's model catalogue"):
            create_tts_engine("omnivoice", model)


def test_the_module_imports_without_mlx() -> None:
    """The registry imports every engine module; MLX is imported in load()."""
    code = "import sys; sys.modules['mlx'] = None; import voiced.engines.omnivoice"
    subprocess.run([sys.executable, "-c", code], check=True)


def test_the_languages_are_the_card_tags_with_the_six_voices_among_them() -> None:
    assert len(LANGUAGES) == len(set(LANGUAGES)) == 646
    assert all(re.fullmatch(r"[a-z]{2,3}", tag) for tag in LANGUAGES), "a tag kept its quotes"
    assert {"no", "yes"} <= set(LANGUAGES)  # Norwegian and Yeskwa: quoted in the card's YAML
    assert set(SIX) <= set(LANGUAGES)
    assert {d.tag for d in DESIGNED} == set(SIX)


def test_the_voice_table() -> None:
    assert OmniVoiceEngine.catalogue_voices() == OMNIVOICE_VOICES
    assert OMNIVOICE_VOICES[0].id == "en-US-female"
    ids = [v.id for v in OMNIVOICE_VOICES]
    assert len(ids) == len(set(ids)) == 6
    by_id = {d.voice.id: d for d in DESIGNED}
    assert by_id["apc-female"].tag == "apc"
    assert by_id["zh-CN-male"].tag == "zh"
    assert by_id["zh-CN-male"].instruct == "男，青年"  # noqa: RUF001 (upstream's form for Chinese text)
    assert voice_label(by_id["apc-female"].voice) == "Young adult (Levantine Arabic, female)"
    assert voice_label(by_id["de-DE-male"].voice) == "Middle-aged (German, male)"


def test_speed_scales_the_length_the_model_fills() -> None:
    normal = frames_to_fill(100, 1.0)
    assert normal in (114, 115)  # 1.15 times, truncated
    assert frames_to_fill(100, 2.0) == normal // 2
    assert frames_to_fill(100, 0.5) in (229, 230)
    assert frames_to_fill(2, 1.0) == omnivoice.MIN_FRAMES


def test_tidy_keeps_a_tenth_of_a_second_of_edge_silence_and_levels_the_peak() -> None:
    quiet = np.full(RATE, 1e-4, dtype=np.float32)  # -80 dBFS
    speech = np.sin(np.linspace(0, 400 * np.pi, RATE // 2)).astype(np.float32) * 0.9
    out = tidy(np.concatenate([quiet, speech, quiet]), RATE)
    assert len(out) == RATE // 2 + 2 * RATE // 10
    assert np.abs(out).max() == pytest.approx(omnivoice.PEAK)
    assert len(tidy(quiet, RATE)) == 0
    assert len(tidy(np.zeros(100, dtype=np.float32), RATE)) == 0


class _StubModel:
    """mlx-audio's OmniVoice as ``_synth`` calls it: tokenization, inputs, the
    forward pass that ``iterative_unmask`` calls, and the codec."""

    config = "cfg"

    def __init__(self, log: list[Any]) -> None:
        self.log = log
        self.text_tokenizer = "tok"
        self.audio_tokenizer = SimpleNamespace(
            decode=lambda tokens: np.full(RATE, 0.25, dtype=np.float32)
        )

    def _tokenize_style_and_text(self, **kwargs: Any) -> tuple[str, str]:
        self.log.append(("tokenize", kwargs))
        return "style", "text"

    def _prepare_inference_inputs(self, style: str, text: str, length: int) -> dict[str, str]:
        self.log.append(("inputs", style, text, length))
        return {"input_ids": "ids", "audio_mask": "mask"}

    def __call__(self, ids: str, mask: str) -> str:
        self.log.append("pass")
        return "logits"


@pytest.fixture
def stub_mlx(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Stub ``mlx`` and mlx-audio's ``iterative_unmask``, which makes 16
    forward passes and records its arguments; ``_synth`` runs without MLX."""
    log: list[Any] = []
    mx = SimpleNamespace(
        eval=lambda x: log.append("eval"),
        float32=np.float32,
        random=SimpleNamespace(seed=lambda n: log.append(("seed", n))),
    )

    def iterative_unmask(model: Any, **kwargs: Any) -> str:
        log.append(("unmask", model.config, kwargs))
        for _ in range(16):
            model(kwargs["cond_input_ids"], kwargs["cond_audio_mask"])
        return "tokens"

    generation = SimpleNamespace(iterative_unmask=iterative_unmask)
    monkeypatch.setitem(sys.modules, "mlx", SimpleNamespace(core=mx))
    monkeypatch.setitem(sys.modules, "mlx.core", mx)
    monkeypatch.setitem(sys.modules, "mlx_audio.tts.models.omnivoice.generation", generation)
    monkeypatch.setattr(omnivoice, "_frames", lambda text: 100.0)
    monkeypatch.setattr(gpu, "Yielder", lambda: lambda: log.append("yield"))
    return log


def test_a_sentence_is_one_unmasking_in_the_voice_with_a_yield_before_each_pass(
    stub_mlx: list[Any],
) -> None:
    model = _StubModel(stub_mlx)
    voice = next(d for d in DESIGNED if d.voice.id == "apc-female")
    pcm = omnivoice._synth(model, "Marhaba.", voice, 2.0, threading.Event())
    assert len(pcm) == 2 * RATE  # the stub codec's second, nothing trimmed
    tokenize, inputs, seed, unmask, *passes, last_yield = stub_mlx
    assert tokenize == (
        "tokenize",
        {
            "text": "Marhaba.",
            "language": "apc",
            "instruct": "female, young adult",
            "text_tokenizer": "tok",
            "denoise": False,
        },
    )
    assert inputs == ("inputs", "style", "text", 57)  # 100 frames * 1.15 / speed 2
    assert seed == ("seed", omnivoice.SEED)
    assert unmask == (
        "unmask",
        "cfg",
        {"cond_input_ids": "ids", "cond_audio_mask": "mask", "T": 57, "num_steps": 16},
    )
    assert passes == ["yield", "pass", "eval"] * 16
    assert last_yield == "yield"  # before the codec


def test_a_stopped_sentence_runs_no_further_pass(
    stub_mlx: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    stop = threading.Event()

    def yielder() -> None:
        if stub_mlx.count("pass") == 3:
            stop.set()

    monkeypatch.setattr(gpu, "Yielder", lambda: yielder)
    with pytest.raises(omnivoice._Stopped):
        omnivoice._synth(_StubModel(stub_mlx), "Hello.", DESIGNED[0], 1.0, stop)
    assert stub_mlx.count("pass") == 3


@pytest.fixture
def fake_runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[tuple[str, str, float]]:
    """A model that records each sentence it is asked for, and a snapshot
    that is a folder under ``tmp_path``."""
    calls: list[tuple[str, str, float]] = []
    loaded: list[str] = []

    def load_model(path: str) -> Any:
        loaded.append(path)
        return SimpleNamespace(sample_rate=RATE)

    def synth(model: Any, sentence: str, voice: Any, speed: float, stop: threading.Event) -> bytes:
        calls.append((sentence, voice.voice.id, speed))
        return bytes(4)

    monkeypatch.setattr(omnivoice, "_load_model", load_model)
    monkeypatch.setattr(omnivoice, "_synth", synth)
    monkeypatch.setattr(models, "snapshot", lambda repo, check_only=False: tmp_path / repo)
    return calls


async def test_load_reads_the_pinned_snapshot_and_warms_up(
    fake_runtime: list[tuple[str, str, float]],
) -> None:
    engine = OmniVoiceEngine()
    await engine.load()
    assert fake_runtime == [("Warming up.", "en-US-female", 1.0)]
    assert await engine.voices() == OMNIVOICE_VOICES


async def test_a_load_with_the_wrong_rate_leaves_it_unloaded(
    fake_runtime: list[tuple[str, str, float]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(omnivoice, "_load_model", lambda path: SimpleNamespace(sample_rate=44_100))
    engine = OmniVoiceEngine()
    with pytest.raises(RuntimeError, match="44100 Hz"):
        await engine.load()
    assert engine._model is None


async def test_each_sentence_goes_to_the_model_in_its_voice_and_speed(
    fake_runtime: list[tuple[str, str, float]],
) -> None:
    engine = OmniVoiceEngine()
    await engine.load()
    fake_runtime.clear()
    chunks = [c async for c in engine.synthesize("Eins. Zwei. ...", "de-DE-male", speed=1.5)]
    assert [c.data for c in chunks] == [bytes(4), bytes(4)]
    # "..." has nothing to say; the model would fill 0.4 s with sound.
    assert fake_runtime == [("Eins.", "de-DE-male", 1.5), ("Zwei.", "de-DE-male", 1.5)]


async def test_an_unknown_voice_never_reaches_the_model(
    fake_runtime: list[tuple[str, str, float]],
) -> None:
    engine = OmniVoiceEngine()
    await engine.load()
    fake_runtime.clear()
    for voice in ("en", "female", "af_heart"):
        with pytest.raises(UnknownVoice):
            await engine.synthesize("Hi.", voice).__anext__()
    assert fake_runtime == []


async def test_a_client_that_leaves_stops_the_sentence_at_its_next_pass(
    fake_runtime: list[tuple[str, str, float]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Leaving mid-sentence calls ``_interrupt``, which sets the sentence's
    stop event: the passes after it never run, and the cancel returns once
    the pass in flight is done."""
    passes: list[int] = []

    def slow(model: Any, sentence: str, voice: Any, speed: float, stop: threading.Event) -> bytes:
        for n in range(32):
            if stop.is_set():
                raise omnivoice._Stopped
            passes.append(n)
            time.sleep(0.02)
        return bytes(4)

    engine = OmniVoiceEngine()
    await engine.load()
    monkeypatch.setattr(omnivoice, "_synth", slow)
    gen = engine.synthesize("A long sentence.", "en-US-female")
    task = asyncio.ensure_future(gen.__anext__())
    await asyncio.sleep(0.15)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    done = len(passes)
    await asyncio.sleep(0.1)
    assert 0 < done < 32
    assert len(passes) == done, "a pass ran after the client left"


async def test_an_http_hang_up_mid_sentence_is_a_disconnect_and_stops_the_passes(
    fake_runtime: list[tuple[str, str, float]],
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
) -> None:
    """Through the speech route, ``HangUp`` and the sentence loop (ADR 0005's
    amendment): a client that hangs up after the first sentence's audio is a
    disconnect, and the second sentence stops at its next pass."""
    from tests.api.conftest import loaded
    from tests.api.test_speech import _asgi_post, _wait_until
    from voiced.tts import build_tts_app

    passes: list[int] = []

    def slow(model: Any, sentence: str, voice: Any, speed: float, stop: threading.Event) -> bytes:
        for n in range(32):
            if stop.is_set():
                raise omnivoice._Stopped
            passes.append(n)
            if sentence != "First.":
                stop.wait(0.05)  # the second sentence runs until the hang-up
        return bytes(4)

    app = build_tts_app(settings, OmniVoiceEngine())
    async with loaded(app):
        monkeypatch.setattr(omnivoice, "_synth", slow)
        disconnect = asyncio.Event()

        async def on_body(body: bytes) -> None:
            if body:
                disconnect.set()  # hang up after the first audio

        request = asyncio.create_task(
            _asgi_post(
                app,
                {"input": "First. And a second sentence.", "voice": "en-US-female"},
                disconnect=disconnect,
                on_body=on_body,
            )
        )
        await asyncio.wait_for(request, 10.0)
        await _wait_until(lambda: app.state.ctx.queue.depth == 0)
        done = len(passes)
        await asyncio.sleep(0.1)
        # The first sentence's 32 passes, and none or a few of the second's.
        assert 32 <= done < 64 and len(passes) == done, "the second sentence stopped"
        counts = app.state.ctx.metrics.snapshot()["operations"]["speech"]["counts"]
        assert counts["disconnect"] == 1


# -- against the real model ----------------------------------------------------


@pytest.mark.model
def test_the_languages_are_the_pinned_card_tags() -> None:
    from huggingface_hub import ModelCard, hf_hub_download

    revision = models.pinned_revision(OmniVoiceEngine.model_id)
    path = hf_hub_download(OmniVoiceEngine.model_id, "README.md", revision=revision)
    # Parsed as YAML: the card quotes "no" and "yes", which YAML 1.1 reads as booleans.
    assert ModelCard.load(path).data.language == LANGUAGES


@pytest.mark.model
@pytest.mark.parametrize(
    "voice, text",
    [
        ("en-US-female", "The quick brown fox jumps over the lazy dog."),
        ("de-DE-male", "Der schnelle braune Fuchs springt über den faulen Hund."),
        ("apc-female", "مرحبا، كيفك؟ وين شغلك؟"),
        ("es-ES-male", "El veloz zorro marrón salta sobre el perro perezoso."),
        ("fr-FR-female", "Le rapide renard brun saute par-dessus le chien paresseux."),
        ("zh-CN-male", "今天天气温和，预计傍晚会有小雨。"),  # noqa: RUF001
    ],
)
def test_synthesizes_plausible_duration(omnivoice_engine: Any, voice: str, text: str) -> None:
    async def collect() -> bytes:
        return b"".join([c.data async for c in omnivoice_engine.synthesize(text, voice)])

    pcm = np.frombuffer(asyncio.run(collect()), dtype="<i2")
    assert 1.0 < len(pcm) / RATE < 6.0, len(pcm) / RATE
    assert np.abs(pcm).max() == pytest.approx(omnivoice.PEAK * 32767, rel=0.01)


@pytest.mark.model
def test_one_piece_per_sentence_in_order_and_the_same_audio_each_time(
    omnivoice_engine: Any,
) -> None:
    """No streaming within a sentence: first audio is the whole first sentence."""
    text = "This is the first sentence. And a second one."

    async def timed() -> list[tuple[float, bytes]]:
        started = time.perf_counter()
        return [
            (time.perf_counter() - started, c.data)
            async for c in omnivoice_engine.synthesize(text, "en-US-female")
        ]

    first, again = asyncio.run(timed()), asyncio.run(timed())
    assert len(first) == 2 and first[0][0] < first[1][0]
    assert [d for _, d in first] == [d for _, d in again]


@pytest.mark.model
def test_speed_shortens_the_audio(omnivoice_engine: Any) -> None:
    async def seconds(speed: float) -> float:
        pcm = b"".join(
            [
                c.data
                async for c in omnivoice_engine.synthesize(
                    "The quick brown fox jumps over the lazy dog.", "en-US-female", speed
                )
            ]
        )
        return len(pcm) / 2 / RATE

    assert asyncio.run(seconds(1.5)) < asyncio.run(seconds(1.0)) * 0.85


@pytest.mark.model
def test_synthesis_waits_while_the_stt_holds_the_gpu(omnivoice_engine: Any) -> None:
    """ADR 0019: held for the whole sentence, the wait is the budget."""

    async def one() -> float:
        started = time.perf_counter()
        async for _ in omnivoice_engine.synthesize("Hello there.", "en-US-female"):
            pass
        return time.perf_counter() - started

    free = asyncio.run(one())
    with gpu.stt_turn():
        held = asyncio.run(one())
    assert held - free > gpu.MAX_WAIT_S * 0.8, (free, held)


@pytest.mark.model
def test_unknown_voice(omnivoice_engine: Any) -> None:
    async def first() -> None:
        await omnivoice_engine.synthesize("Hi.", "nobody").__anext__()

    with pytest.raises(UnknownVoice):
        asyncio.run(first())


@pytest.fixture
def settings(settings: Settings) -> Settings:
    """The six voices' languages, and one the model does not speak."""
    settings.tts.languages = ["en", "de", "apc", "es", "fr", "zh", "tlh"]
    return settings


@pytest.fixture
def tts_engine() -> OmniVoiceEngine:
    """The engine the ``tts_client`` app loads (``tests/conftest.py``)."""
    return OmniVoiceEngine()


@pytest.mark.model
async def test_voices_and_models_over_http(tts_client: AsyncClient) -> None:
    voices = (await tts_client.get("/v1/audio/voices")).json()["data"]
    assert [v["id"] for v in voices] == [v.id for v in OMNIVOICE_VOICES]
    apc = next(v for v in voices if v["id"] == "apc-female")
    assert (apc["language"], apc["gender"]) == ("apc", "female")
    (model,) = (await tts_client.get("/v1/models")).json()["data"]
    assert (model["id"], model["engine"]) == ("k2-fsa/OmniVoice", "omnivoice")
    assert "CC-BY-NC" in model["license"]
    assert set(model["languages"]) == {"en", "de", "apc", "es", "fr", "zh"}  # not "tlh"
