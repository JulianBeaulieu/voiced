"""Chatterbox Turbo engine tests. The shared contract tests are in
``test_sentence_engines.py``; these cover what is Chatterbox's own, first
without MLX (they run on CI), then against the real model (``model``)."""

from __future__ import annotations

import asyncio
import logging
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
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
    CROSSFADE_SAMPLES,
    DEFAULT_VOICE,
    FIRST_TOKENS,
    MODELS,
    S3_TOKENIZER_REPO,
    SILENCE_TOKENS,
    ChatterboxEngine,
    SpeechTokenizerSkipped,
    max_tokens,
    speak,
)
from voiced.engines.registry import ModelRefused, get_tts_engine
from voiced.text import normalize
from voiced.text.sentences import split_sentences

SILENCE = 4299
"""What the stand-ins use for S3Gen's silence token."""
SAMPLES_PER_TOKEN = 960
"""What the stand-in S3Gen renders per token: 40 ms."""


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


@pytest.mark.parametrize(
    "model", ["mlx-community/chatterbox-fp16", "mlx-community/chatterbox-turbo-8bit"]
)
def test_other_chatterbox_models_are_refused(model: str) -> None:
    """Not in the catalogue: the original Chatterbox has no Turbo token
    stream, and the other conversions are not pinned."""
    with pytest.raises(ModelRefused, match="not in voiced's model catalogue"):
        ChatterboxEngine(model)
    assert ChatterboxEngine("/some/local/folder").model_id == "/some/local/folder"


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


def test_tags_survive_normalization_and_the_sentence_split() -> None:
    """The model performs ``[laugh]`` and its kind; voiced must hand them over."""
    text = normalize("Oh, that is hilarious! [chuckle] Um, anyway. [laugh] Yes.", language="en")
    assert split_sentences(text) == [
        "Oh, that is hilarious!",
        "[chuckle] Um, anyway.",
        "[laugh] Yes.",
    ]
    assert normalize("[sigh] Fine.", language="en") == "[sigh] Fine."
    assert normalize("[3] more", language="en") == "[three] more."


# -- the streaming schedule ----------------------------------------------------


class _Run:
    """A sentence of ``n`` tokens from a stand-in T3 and S3Gen, with every
    wait, step and render recorded in order. Each render of ``k`` tokens is
    ``k * SAMPLES_PER_TOKEN`` samples of the render's number, so a test can
    tell which render a sample came from."""

    def __init__(self, n: int) -> None:
        self.n = n
        self.log: list[str] = []
        self.renders: list[tuple[int, int, bool]] = []

    def steps(self) -> Iterator[list[int]]:
        for i in range(self.n):
            self.log.append("step")
            yield [i]

    def render(self, tokens: list[int], start: int, final: bool) -> tuple[NDArray[np.float32], int]:
        self.log.append("render")
        self.renders.append((len(tokens), start, final))
        total = len(tokens) * SAMPLES_PER_TOKEN
        return np.full(total - start, float(len(self.renders)), dtype=np.float32), total

    def wait(self) -> None:
        self.log.append("wait")

    def pieces(self) -> list[NDArray[np.float32]]:
        return list(speak(self.steps(), self.render, self.wait, SILENCE))


def test_renders_the_first_tokens_early_then_in_larger_chunks() -> None:
    run = _Run(FIRST_TOKENS + 2 * CHUNK_TOKENS + 3)
    pieces = run.pieces()
    first, second = FIRST_TOKENS, FIRST_TOKENS + CHUNK_TOKENS
    third, final = FIRST_TOKENS + 2 * CHUNK_TOKENS, run.n + SILENCE_TOKENS
    held = CROSSFADE_SAMPLES
    assert run.renders == [
        (first, 0, False),
        (second, first * SAMPLES_PER_TOKEN - held, False),
        (third, second * SAMPLES_PER_TOKEN - held, False),
        (final, third * SAMPLES_PER_TOKEN - held, True),
    ]
    # Together the pieces are the whole sentence, each sample sent once.
    assert sum(len(p) for p in pieces) == final * SAMPLES_PER_TOKEN


def test_each_join_is_crossfaded_from_one_render_into_the_next() -> None:
    """Renders draw fresh noise, so render 2's version of the held-back tail
    differs from render 1's: the join must ramp from one to the other."""
    run = _Run(FIRST_TOKENS + 3)
    audio = np.concatenate(run.pieces())
    join = FIRST_TOKENS * SAMPLES_PER_TOKEN - CROSSFADE_SAMPLES
    assert np.all(audio[:join] == 1.0)  # render 1
    fade = audio[join : join + CROSSFADE_SAMPLES]
    assert fade[0] == pytest.approx(1.0) and fade[-1] == pytest.approx(2.0)
    assert np.all(np.diff(fade) >= 0)  # a ramp, no jump
    assert np.all(audio[join + CROSSFADE_SAMPLES :] == 2.0)  # render 2
    assert np.abs(np.diff(audio)).max() < 0.01


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
    run.render = lambda t, start, final: (tokens.append(list(t)), (np.zeros(0, np.float32), 0))[1]  # type: ignore[method-assign]
    run.pieces()
    assert tokens == [[0, 1, 2] + [SILENCE] * SILENCE_TOKENS]


def test_nothing_to_say_renders_nothing() -> None:
    run = _Run(0)
    assert run.pieces() == []
    assert run.renders == []


def test_empty_renders_are_not_yielded() -> None:
    run = _Run(FIRST_TOKENS + 1)
    run.render = lambda t, start, final: (np.zeros(0, np.float32), start)  # type: ignore[method-assign]
    assert run.pieces() == []


# -- _stream, against stand-ins for MLX and mlx-audio --------------------------


class _T3:
    def __init__(self, n: int) -> None:
        self.n = n
        self.kwargs: dict[str, Any] = {}
        self.threads: set[int] = set()

    def inference_turbo_stream(self, **kwargs: Any) -> Iterator[tuple[np.ndarray, bool]]:
        self.kwargs = kwargs
        for i in range(min(self.n, kwargs["max_gen_len"])):
            self.threads.add(threading.get_ident())  # T3 keeps its KV cache between steps
            yield np.array([[i]]), False


class _S3Gen:
    def __init__(self) -> None:
        self.threads: set[int] = set()

    def inference_stream(
        self, *, speech_tokens: np.ndarray, prev_audio_samples: int, **_: Any
    ) -> tuple[np.ndarray, int]:
        self.threads.add(threading.get_ident())
        total = speech_tokens.shape[-1] * SAMPLES_PER_TOKEN
        return np.zeros((1, max(0, total - prev_audio_samples)), dtype=np.float32), total


@pytest.fixture
def fake_mlx(monkeypatch: pytest.MonkeyPatch) -> None:
    """``mlx.core`` and the mlx-audio modules ``_stream`` imports."""
    core = SimpleNamespace(array=lambda x, dtype=None: np.array(x), int32=np.int32)
    names = {
        "mlx": SimpleNamespace(core=core),
        "mlx.core": core,
        "mlx_audio": SimpleNamespace(),
        "mlx_audio.tts": SimpleNamespace(),
        "mlx_audio.tts.models": SimpleNamespace(),
        "mlx_audio.tts.models.chatterbox_turbo": SimpleNamespace(),
        "mlx_audio.tts.models.chatterbox_turbo.chatterbox_turbo": SimpleNamespace(
            punc_norm=lambda text: text
        ),
        "mlx_audio.tts.models.chatterbox_turbo.models": SimpleNamespace(),
        "mlx_audio.tts.models.chatterbox_turbo.models.s3gen": SimpleNamespace(),
        "mlx_audio.tts.models.chatterbox_turbo.models.s3gen.s3gen": SimpleNamespace(
            S3GEN_SIL=SILENCE, SPEECH_VOCAB_SIZE=6561
        ),
    }
    for name, module in names.items():
        monkeypatch.setitem(sys.modules, name, module)


def _model(t3: _T3) -> Any:
    return SimpleNamespace(
        tokenizer=lambda text, return_tensors: SimpleNamespace(input_ids=np.array([[1, 2]])),
        _conds=SimpleNamespace(t3="t3 cond", gen="gen cond"),
        t3=t3,
        s3gen=_S3Gen(),
    )


@pytest.mark.usefixtures("fake_mlx")
def test_t3_is_capped_at_the_sentence_bound(caplog: pytest.LogCaptureFixture) -> None:
    sentence = "Hello there."
    t3 = _T3(n=20)
    with caplog.at_level(logging.WARNING, logger="voiced.engines.chatterbox"):
        audio = np.concatenate(list(chatterbox._stream(_model(t3), sentence)))
    assert t3.kwargs["max_gen_len"] == max_tokens(sentence)
    assert t3.kwargs["chunk_size"] == 1
    assert audio.size == (20 + SILENCE_TOKENS) * SAMPLES_PER_TOKEN
    assert not caplog.records  # stopped on its own


@pytest.mark.usefixtures("fake_mlx")
def test_a_runaway_cut_at_the_bound_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    t3 = _T3(n=10_000)
    with caplog.at_level(logging.WARNING, logger="voiced.engines.chatterbox"):
        list(chatterbox._stream(_model(t3), "Hi."))
    assert [r.getMessage() for r in caplog.records] == [
        "chatterbox: runaway generation for 'Hi.': cut at the 34-token bound"
    ]


@pytest.mark.usefixtures("fake_mlx")
async def test_every_step_of_a_sentence_runs_on_the_engine_thread() -> None:
    """T3 holds unevaluated MLX arrays from one token to the next, and MLX
    arrays belong to the thread that made them: a sentence whose steps ran on
    two threads of a busy shared pool crashed. Every step and render of every
    sentence must run on the engine's one thread."""
    model = _model(_T3(n=FIRST_TOKENS + CHUNK_TOKENS + 5))
    engine = ChatterboxEngine()
    engine._model = model

    async def busy_pool() -> None:
        await asyncio.gather(*(asyncio.to_thread(time.sleep, 0.002) for _ in range(8)))

    async def speak_twice() -> None:
        for _ in range(2):
            async for _chunk in engine.synthesize("One sentence. And another one.", "default"):
                await asyncio.sleep(0)

    await asyncio.gather(busy_pool(), speak_twice(), busy_pool())
    threads = model.t3.threads | model.s3gen.threads
    assert len(threads) == 1
    assert threading.get_ident() not in threads


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


def test_mlx_audios_note_about_the_refused_tokenizer_is_dropped(
    caplog: pytest.LogCaptureFixture,
) -> None:
    hook_log = logging.getLogger("mlx_audio.tts.models.chatterbox_turbo.chatterbox_turbo")
    with caplog.at_level(logging.INFO):
        with chatterbox._without_speech_tokenizer():
            hook_log.warning("Could not load S3 speech tokenizer: not loaded")
            hook_log.warning("Failed to load conds.safetensors: broken")
        hook_log.warning("Could not load S3 speech tokenizer: later")
    assert [r.getMessage() for r in caplog.records] == [
        "Failed to load conds.safetensors: broken",
        "Could not load S3 speech tokenizer: later",
    ]


# -- load, without MLX ---------------------------------------------------------


@pytest.fixture
def fake_load(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """``mlx_audio.tts.utils.load_model``, ``load_pinned`` and ``_stream``
    stand-ins; the models built."""
    built: list[Any] = []

    def load_model(repo: str) -> Any:
        with pytest.raises(SpeechTokenizerSkipped):  # what the hook would meet
            huggingface_hub.hf_hub_download(S3_TOKENIZER_REPO, "model.safetensors")
        model = SimpleNamespace(
            repo=repo, sample_rate=TTS_SAMPLE_RATE, _conds=object(), _s3tokenizer=object()
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
    monkeypatch.setattr(models, "load_pinned", lambda loader, model_id: loader(model_id))
    return built


async def test_load_reads_the_pinned_model_and_drops_the_speech_tokenizer(
    fake_load: list[Any],
) -> None:
    engine = ChatterboxEngine("mlx-community/chatterbox-turbo-fp16")
    await engine.load()
    (model,) = fake_load
    assert model.repo == "mlx-community/chatterbox-turbo-fp16"
    assert model._s3tokenizer is None
    assert model.said == ["Warming up."]
    assert await engine.voices() == [DEFAULT_VOICE]


@pytest.mark.parametrize(
    "spoil, message",
    [
        (lambda m: setattr(m, "sample_rate", 44_100), "44100 Hz"),
        (lambda m: setattr(m, "_conds", None), "no default voice"),
        (lambda m: delattr(m, "_conds"), "no default voice"),  # the hook never set it
    ],
)
async def test_a_load_that_fails_leaves_it_unloaded(
    fake_load: list[Any], monkeypatch: pytest.MonkeyPatch, spoil: Any, message: str
) -> None:
    utils = sys.modules["mlx_audio.tts.utils"]
    load = utils.load_model  # type: ignore[attr-defined]
    monkeypatch.setattr(utils, "load_model", lambda repo: (m := load(repo), spoil(m))[0])
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
