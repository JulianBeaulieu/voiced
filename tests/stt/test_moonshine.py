"""Moonshine engine tests: first with a stand-in ``moonshine_voice`` module
(they run on CI), then against the real runtime (``model``)."""

from __future__ import annotations

import asyncio
import enum
import subprocess
import sys
import threading
import time
import types
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from httpx import AsyncClient
from numpy.typing import NDArray

from voiced import models
from voiced.engines import moonshine
from voiced.engines.base import AudioInput, EngineError, EngineNotLoaded, UnsupportedLanguage
from voiced.engines.moonshine import MODELS, MoonshineEngine, model_folder
from voiced.engines.registry import ModelRefused, get_stt_engine

from .conftest import word_accuracy

REFERENCE = "Turn on the kitchen lights and set the thermostat to twenty degrees."


def test_registered_with_its_models_and_licence(tmp_path: Path) -> None:
    assert get_stt_engine("moonshine") is MoonshineEngine
    assert MoonshineEngine().model_id == "moonshine-ai/moonshine-streaming-small"
    assert MoonshineEngine.license == "MIT"
    assert MoonshineEngine.max_streams == 1
    for model in MODELS:
        assert MoonshineEngine(model).model_id == model
    assert MoonshineEngine(str(tmp_path)).model_id == str(tmp_path)  # a local folder
    with pytest.raises(ModelRefused, match="not in voiced's model catalogue"):
        MoonshineEngine("moonshine-ai/moonshine-tiny-ko")


def test_each_model_is_one_folder_of_one_pinned_repository() -> None:
    catalogued = [models.find(m) for m in MODELS]
    assert {found[1].repo for found in catalogued if found} == {
        "moonshine-ai/moonshine-voice-assets"
    }
    assert len({model_folder(m) for m in MODELS}) == len(MODELS)
    for model in MODELS:
        locked = models.locked(model)
        assert locked is not None
        assert all(f.startswith(model_folder(model) + "/") for f in locked.files)
        assert not any("with_attention" in f for f in locked.files)  # word timestamps are off


def test_the_module_imports_without_moonshine_voice() -> None:
    """The registry imports every engine module; the runtime is imported in load()."""
    code = "import sys; sys.modules['moonshine_voice'] = None; import voiced.engines.moonshine"
    subprocess.run([sys.executable, "-c", code], check=True)


@pytest.mark.parametrize(
    ("system", "release", "says"),
    [("Darwin", "23.6.0", "macOS 15"), ("Darwin", "24.6.0", "moonshine extra")],
)
async def test_without_the_runtime_load_says_what_is_missing(
    monkeypatch: pytest.MonkeyPatch, system: str, release: str, says: str
) -> None:
    monkeypatch.setitem(sys.modules, "moonshine_voice", None)
    monkeypatch.setattr(moonshine.platform, "system", lambda: system)
    monkeypatch.setattr(moonshine.platform, "release", lambda: release)
    engine = MoonshineEngine()
    with pytest.raises(RuntimeError, match=says):
        await engine.load()
    with pytest.raises(EngineNotLoaded):
        await engine.stream()


# --- a stand-in moonshine_voice ----------------------------------------------


class _Arch(enum.IntEnum):
    TINY_STREAMING = 2
    SMALL_STREAMING = 4
    MEDIUM_STREAMING = 5


@dataclass
class _Line:
    text: str
    start_time: float
    duration: float
    line_id: int


@dataclass
class _Result:
    lines: list[_Line]


@dataclass
class _Event:
    line: _Line


@dataclass
class _ErrorEvent:
    error: Exception


def _on_the_model_thread() -> None:
    """The runtime is called on the engine's thread, the list conversion of
    the samples included, never on the event loop."""
    assert threading.current_thread().name.startswith("moonshine")


@dataclass
class _FakeStream:
    """Decodes every 0.5 s of audio into one line of words, one per pass."""

    lines: list[_Line] = field(default_factory=list)
    listeners: list[Callable[[Any], None]] = field(default_factory=list)
    log: list[str] = field(default_factory=list)
    samples: int = 0
    fail_stop: bool = False

    def add_listener(self, listener: Callable[[Any], None]) -> None:
        self.listeners.append(listener)

    def start(self) -> None:
        self.log.append("start")

    def add_audio(self, audio: list[float], sample_rate: int) -> None:
        _on_the_model_thread()
        assert isinstance(audio, list) and sample_rate == 16_000
        before, self.samples = self.samples, self.samples + len(audio)
        if self.samples // 8000 > before // 8000:
            self._pass()

    def _pass(self) -> None:
        words = " ".join(["word"] * (self.samples // 8000))
        line = _Line(words, 0.0, self.samples / 16_000, 7)
        self.lines = [line]
        for listener in self.listeners:
            listener(_Event(line))

    def stop(self) -> _Result | None:
        self.log.append("stop")
        if self.fail_stop:
            for listener in self.listeners:
                listener(_ErrorEvent(RuntimeError("pass failed")))
            return None
        done = _Line("final words.", 0.0, self.samples / 16_000 + 0.5, 7)
        return _Result([done, _Line("  ", 9.0, 1.0, 8)])

    def close(self) -> None:
        self.log.append("close")


class _FakeTranscriber:
    def __init__(self, root: str, arch: _Arch, update_interval: float) -> None:
        self.root, self.arch, self.update_interval = root, arch, update_interval
        self.streams: list[_FakeStream] = []
        self.batches: list[int] = []

    def transcribe_without_streaming(self, audio: list[float], sample_rate: int) -> _Result:
        _on_the_model_thread()
        assert isinstance(audio, list) and sample_rate == 16_000
        self.batches.append(len(audio))
        return _Result([_Line(" Hello  there. ", 0.25, 1.0, 1), _Line("Again.", 1.5, 9.0, 2)])

    def create_stream(self, update_interval: float) -> _FakeStream:
        stream = _FakeStream()
        self.streams.append(stream)
        return stream


@dataclass
class _Runtime:
    built: list[_FakeTranscriber] = field(default_factory=list)
    snapshots: list[str] = field(default_factory=list)


@pytest.fixture
def fake_runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> _Runtime:
    runtime = _Runtime()

    def transcriber(root: str, arch: _Arch, update_interval: float) -> _FakeTranscriber:
        t = _FakeTranscriber(root, arch, update_interval)
        runtime.built.append(t)
        return t

    module = types.ModuleType("moonshine_voice")
    module.ModelArch = _Arch  # type: ignore[attr-defined]
    module.Transcriber = transcriber  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "moonshine_voice", module)

    def snapshot(model_id: str, *, check_only: bool = False) -> Path:
        assert check_only  # the services never download
        runtime.snapshots.append(model_id)
        local = Path(model_id)
        return local if local.is_dir() else tmp_path

    monkeypatch.setattr(models, "snapshot", snapshot)
    return runtime


async def _loaded(fake_runtime: _Runtime) -> tuple[MoonshineEngine, _FakeTranscriber]:
    engine = MoonshineEngine()
    await engine.load()
    return engine, fake_runtime.built[-1]


def _audio(seconds: float) -> NDArray[np.float32]:
    return np.full(round(seconds * 16_000), 0.1, dtype=np.float32)


async def test_load_reads_the_pinned_folder_and_warms_both_paths(
    fake_runtime: _Runtime, tmp_path: Path
) -> None:
    engine, transcriber = await _loaded(fake_runtime)
    assert fake_runtime.snapshots == [engine.model_id]
    assert transcriber.root == str(tmp_path / model_folder(engine.model_id))
    assert transcriber.arch is _Arch.SMALL_STREAMING
    assert transcriber.update_interval == moonshine.UPDATE_S
    assert transcriber.batches == [16_000]
    assert transcriber.streams[0].log == ["start", "stop", "close"]


async def test_a_local_folder_loads_as_it_is(fake_runtime: _Runtime, tmp_path: Path) -> None:
    engine = MoonshineEngine(str(tmp_path))
    await engine.load()
    assert fake_runtime.built[-1].root == str(tmp_path)
    assert fake_runtime.built[-1].arch is _Arch.SMALL_STREAMING


async def test_a_load_that_fails_leaves_it_unloaded(
    fake_runtime: _Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(self: _FakeTranscriber, audio: list[float], rate: int) -> _Result:
        raise RuntimeError("bad model")

    monkeypatch.setattr(_FakeTranscriber, "transcribe_without_streaming", broken)
    engine = MoonshineEngine()
    with pytest.raises(RuntimeError, match="bad model"):
        await engine.load()
    with pytest.raises(EngineNotLoaded):
        await engine.transcribe(AudioInput(_audio(1)))


async def test_a_warm_up_whose_last_pass_fails_fails_the_load(
    fake_runtime: _Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``Stream.stop()`` returns None for a failed pass, and the warm-up
    stream has no listener to hear why."""

    def failing(self: _FakeTranscriber, update_interval: float) -> _FakeStream:
        stream = _FakeStream(fail_stop=True)
        self.streams.append(stream)
        return stream

    monkeypatch.setattr(_FakeTranscriber, "create_stream", failing)
    engine = MoonshineEngine()
    with pytest.raises(RuntimeError, match="no transcript"):
        await engine.load()
    with pytest.raises(EngineNotLoaded):
        await engine.stream()
    assert fake_runtime.built[-1].streams[-1].log == ["start", "stop", "close"]


async def test_silence_is_not_transcribed(fake_runtime: _Runtime) -> None:
    engine, transcriber = await _loaded(fake_runtime)
    batches = len(transcriber.batches)
    result = await engine.transcribe(AudioInput(np.zeros(32_000, dtype=np.float32)))
    assert (result.text, result.segments, result.duration_s) == ("", (), 2.0)
    assert len(transcriber.batches) == batches


async def test_batch_lines_become_segments_clipped_to_the_input(
    fake_runtime: _Runtime,
) -> None:
    engine, _ = await _loaded(fake_runtime)
    result = await engine.transcribe(AudioInput(_audio(3)), "en-US")
    assert result.text == "Hello there. Again."
    assert result.language == "en" and result.duration_s == 3.0
    assert [(s.start, s.end, s.text) for s in result.segments] == [
        (0.25, 1.25, "Hello there."),
        (1.5, 3.0, "Again."),
    ]
    empty = await engine.transcribe(AudioInput(np.zeros(0, dtype=np.float32)))
    assert empty.text == "" and empty.segments == ()


async def test_bad_input_is_refused(fake_runtime: _Runtime) -> None:
    engine, _ = await _loaded(fake_runtime)
    with pytest.raises(UnsupportedLanguage):
        await engine.transcribe(AudioInput(_audio(1)), "de")
    with pytest.raises(UnsupportedLanguage):
        await engine.stream("fr")
    with pytest.raises(EngineError, match="Hz"):
        await engine.transcribe(AudioInput(_audio(1), sample_rate=8000))
    with pytest.raises(EngineError, match="mono"):
        await engine.transcribe(AudioInput(np.zeros((2, 10), dtype=np.float32)))


async def test_a_stream_decodes_as_audio_comes_and_finalizes_on_stop(
    fake_runtime: _Runtime,
) -> None:
    engine, transcriber = await _loaded(fake_runtime)
    async with await engine.stream() as session:
        assert len(transcriber.streams) == 1  # opening a session creates nothing
        assert session.partial() == ""
        for _ in range(10):
            await session.add_audio(_audio(0.1))
        assert session.partial() == "word word"  # each add_audio waits for its pass
        result = await session.finalize()
        stream = transcriber.streams[-1]
        assert stream.samples == 16_000
        assert stream.log == ["start", "stop"]
        assert result.text == "final words."
        assert [(s.start, s.end) for s in result.segments] == [(0.0, 1.0)]
        with pytest.raises(EngineError, match="closed"):
            await session.add_audio(_audio(0.1))
    assert stream.log == ["start", "stop", "close"]


async def test_finalize_without_audio_is_empty(fake_runtime: _Runtime) -> None:
    engine, transcriber = await _loaded(fake_runtime)
    async with await engine.stream() as session:
        result = await session.finalize()
    assert result.text == "" and result.duration_s == 0.0
    assert len(transcriber.streams) == 1  # the warm-up's only


async def test_a_failed_last_pass_is_an_engine_failure(
    fake_runtime: _Runtime,
) -> None:
    engine, transcriber = await _loaded(fake_runtime)
    async with await engine.stream() as session:
        await session.add_audio(_audio(0.2))
        transcriber.streams[-1].fail_stop = True
        with pytest.raises(RuntimeError, match="pass failed"):
            await session.finalize()
    assert transcriber.streams[-1].log[-1] == "close"


async def test_closing_without_finalize_discards_the_audio(
    fake_runtime: _Runtime,
) -> None:
    engine, transcriber = await _loaded(fake_runtime)
    session = await engine.stream()
    await session.add_audio(_audio(0.2))
    await session.close()
    await session.close()  # idempotent
    assert transcriber.streams[-1].log == ["start", "close"]


# --- the real runtime ---------------------------------------------------------


@pytest.fixture(scope="module")
def moonshine_engines() -> dict[str, MoonshineEngine]:
    """Each model, loaded once for the module; skipped where the runtime is
    not installed (macOS 14, or without the moonshine extra)."""
    pytest.importorskip("moonshine_voice")
    loaded = {}
    for model in MODELS:
        engine = MoonshineEngine(model)
        asyncio.run(engine.load())
        loaded[model] = engine
    return loaded


@pytest.mark.model
@pytest.mark.parametrize("model", list(MODELS))
def test_transcribes_the_fixture(
    moonshine_engines: dict[str, MoonshineEngine], model: str, speech: NDArray[np.float32]
) -> None:
    result = asyncio.run(moonshine_engines[model].transcribe(AudioInput(speech)))
    assert word_accuracy(result.text, REFERENCE) >= 0.8, result.text
    assert result.segments and 0.0 <= result.segments[0].start < result.segments[-1].end


@pytest.mark.model
@pytest.mark.parametrize("tail_ms", [0, 200], ids=["at-the-last-word", "after-200ms-silence"])
@pytest.mark.parametrize("chunk", [1024, 1600], ids=["64ms", "100ms"])
@pytest.mark.parametrize("model", list(MODELS))
def test_streams_in_real_time_and_finalizes_on_the_end_signal(
    moonshine_engines: dict[str, MoonshineEngine],
    model: str,
    chunk: int,
    tail_ms: int,
    speech: NDArray[np.float32],
) -> None:
    """ADR 0008's paths: partials grow while the clip plays; the transcript
    arrives on finalize, not on Moonshine's own end of phrase."""
    audio = np.concatenate([speech, np.zeros(tail_ms * 16, dtype=np.float32)])

    async def run() -> tuple[list[str], str, float]:
        partials: list[str] = []
        async with await moonshine_engines[model].stream() as session:
            for start in range(0, len(audio), chunk):  # paced in real time
                await session.add_audio(audio[start : start + chunk])
                await asyncio.sleep(chunk / 16_000)
                partials.append(session.partial())
            begun = time.perf_counter()
            result = await session.finalize()
            return partials, result.text, time.perf_counter() - begun

    partials, text, finalize_s = asyncio.run(run())
    assert any(partials) and partials[0] == ""
    assert word_accuracy(text, REFERENCE) >= 0.8, text
    assert finalize_s < 1.0  # ADR 0024 has the measured numbers


@pytest.mark.model
@pytest.mark.parametrize("model", list(MODELS))
def test_pure_silence_is_an_empty_transcript(
    moonshine_engines: dict[str, MoonshineEngine], model: str
) -> None:
    silence = np.zeros(32_000, dtype=np.float32)

    async def run() -> tuple[str, str]:
        engine = moonshine_engines[model]
        batch = await engine.transcribe(AudioInput(silence))
        async with await engine.stream() as session:
            for start in range(0, len(silence), 1600):
                await session.add_audio(silence[start : start + 1600])
            streamed = await session.finalize()
        return batch.text, streamed.text

    assert asyncio.run(run()) == ("", "")


@pytest.fixture
def stt_engine(moonshine_engines: dict[str, MoonshineEngine]) -> MoonshineEngine:
    """The engine the ``stt_client`` app serves (``tests/conftest.py``)."""
    return moonshine_engines[MoonshineEngine.model_id]


@pytest.mark.model
async def test_the_model_over_http(stt_client: AsyncClient) -> None:
    (model,) = (await stt_client.get("/v1/models")).json()["data"]
    assert (model["id"], model["engine"], model["license"]) == (
        "moonshine-ai/moonshine-streaming-small",
        "moonshine",
        "MIT",
    )
