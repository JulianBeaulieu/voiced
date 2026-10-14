from __future__ import annotations

import numpy as np
import pytest

from voiced.engines import (
    AudioInput,
    STTEngine,
    UnknownEngine,
    UnknownVoice,
    get_stt_engine,
    get_tts_engine,
    list_stt_engines,
    list_tts_engines,
    register_stt_engine,
)
from voiced.engines.base import STT_SAMPLE_RATE, TTS_SAMPLE_RATE
from voiced.testing import FakeSTTEngine, FakeTTSEngine


def test_entry_points_find_fakes() -> None:
    assert list_stt_engines()["fake"] is FakeSTTEngine
    assert list_tts_engines()["fake"] is FakeTTSEngine
    assert get_stt_engine("fake") is FakeSTTEngine
    assert get_tts_engine("fake") is FakeTTSEngine


def test_unknown_engine_lists_available() -> None:
    with pytest.raises(UnknownEngine, match="available: fake"):
        get_tts_engine("nope")


def test_local_registration_shadows_entry_point() -> None:
    class Other(FakeSTTEngine):
        name = "other"

    register_stt_engine("other", Other)
    assert get_stt_engine("other") is Other


def test_concrete_engine_must_declare_metadata() -> None:
    with pytest.raises(TypeError, match="license"):

        class Incomplete(STTEngine):
            name = "x"
            model_id = "x"
            languages = ["en"]

            async def load(self) -> None: ...

            async def transcribe(self, audio: AudioInput, language: str | None = None):  # type: ignore[no-untyped-def]
                raise NotImplementedError

            async def stream(self, language: str | None = None):  # type: ignore[no-untyped-def]
                raise NotImplementedError

        Incomplete()


async def test_fake_stt_contract() -> None:
    engine = FakeSTTEngine()
    await engine.load()
    one_second = AudioInput(np.ones(STT_SAMPLE_RATE, dtype=np.float32) * 0.1)
    t = await engine.transcribe(one_second)
    assert t.text == "hello world"
    assert t.duration_s == pytest.approx(1.0)
    assert t.segments[0].end == pytest.approx(1.0)

    async with await engine.stream() as session:
        assert session.partial() == ""
        await session.add_audio(one_second.samples)
        assert session.partial() == "hello"
        final = await session.finalize()
    assert final.text == "hello world"
    assert final.duration_s == pytest.approx(1.0)


async def test_fake_tts_contract() -> None:
    engine = FakeTTSEngine(model_id="custom/model")
    assert engine.model_id == "custom/model"
    await engine.load()
    voices = await engine.voices()
    assert any(v.id == "af_heart" for v in voices)
    chunks = [c async for c in engine.synthesize("Hello there. Second one.", "af_heart")]
    assert len(chunks) == 2
    assert all(c.sample_rate == TTS_SAMPLE_RATE for c in chunks)
    assert chunks[0].duration_s > 0
    with pytest.raises(UnknownVoice):
        async for _ in engine.synthesize("x", "no_such_voice"):
            pass
