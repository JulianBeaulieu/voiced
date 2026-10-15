"""Live Wyoming round trips with the ``wyoming`` client library.

The message sequences are the ones Home Assistant sends (ADR 0011 and the
handlers): for STT ``Transcribe`` then ``AudioStart``/``AudioChunk``/
``AudioStop`` answered by one ``Transcript``; for TTS both the plain
``Synthesize`` and the streaming ``SynthesizeStart``/``SynthesizeChunk``/
``SynthesizeStop`` shape (HA >= 2025.7), including the compatibility
``Synthesize`` copy HA sends mid-stream.
"""

from __future__ import annotations

import asyncio

import pytest
from wyoming.asr import Transcribe, Transcript
from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.client import AsyncTcpClient
from wyoming.event import Event
from wyoming.info import Describe, Info
from wyoming.tts import (
    Synthesize,
    SynthesizeChunk,
    SynthesizeStart,
    SynthesizeStop,
    SynthesizeStopped,
    SynthesizeVoice,
)

from tests.e2e.conftest import (
    EXPECTED_WORDS,
    MIN_AUDIO_BYTES,
    TRAILING_WORDS,
    LiveService,
    pcm16_to_wav,
    read_wav_pcm16,
    silence_pcm16,
)
from tests.stt.conftest import words
from voiced.engines.base import STT_SAMPLE_RATE, TTS_SAMPLE_RATE

pytestmark = pytest.mark.model

STT_FORMAT = {"rate": STT_SAMPLE_RATE, "width": 2, "channels": 1}
TTS_FORMAT = {"rate": TTS_SAMPLE_RATE, "width": 2, "channels": 1}
READ_TIMEOUT_S = 30.0


async def _read_until(client: AsyncTcpClient, stop: type[Event]) -> list[Event]:
    """Collect events until one of type ``stop`` (inclusive), bounded in time."""
    events: list[Event] = []
    async with asyncio.timeout(READ_TIMEOUT_S):
        while True:
            event = await client.read_event()
            if event is None:
                raise AssertionError(f"connection closed before {stop.__name__}")
            events.append(event)
            if stop.is_type(event.type):
                return events


async def test_stt_round_trip(stt_service: LiveService) -> None:
    samples, rate = read_wav_pcm16("speech_en.wav")
    audio = samples.tobytes() + silence_pcm16(0.2, rate)
    async with AsyncTcpClient(stt_service.wyoming_host, stt_service.wyoming_port) as client:
        await client.write_event(Transcribe(language="en").event())
        await client.write_event(AudioStart(**STT_FORMAT).event())
        for offset in range(0, len(audio), 2048):  # HA sends ~2 KB (64 ms) chunks
            await client.write_event(
                AudioChunk(audio=audio[offset : offset + 2048], **STT_FORMAT).event()
            )
        await client.write_event(AudioStop().event())
        events = await _read_until(client, Transcript)
    transcript = Transcript.from_event(events[-1])
    assert EXPECTED_WORDS <= set(words(transcript.text)), transcript.text


async def test_stt_without_audio_start_uses_the_chunk_format(stt_service: LiveService) -> None:
    """Some satellites skip AudioStart; the first chunk's format is used."""
    samples, _ = read_wav_pcm16("speech_en_trailing_silence.wav")
    audio = samples.tobytes()
    async with AsyncTcpClient(stt_service.wyoming_host, stt_service.wyoming_port) as client:
        await client.write_event(Transcribe(language="en").event())
        for offset in range(0, len(audio), 2048):
            await client.write_event(
                AudioChunk(audio=audio[offset : offset + 2048], **STT_FORMAT).event()
            )
        await client.write_event(AudioStop().event())
        events = await _read_until(client, Transcript)
    transcript = Transcript.from_event(events[-1])
    assert TRAILING_WORDS <= set(words(transcript.text)), transcript.text


async def test_stt_describe_returns_info(stt_service: LiveService) -> None:
    async with AsyncTcpClient(stt_service.wyoming_host, stt_service.wyoming_port) as client:
        await client.write_event(Describe().event())
        events = await _read_until(client, Info)
    info = Info.from_event(events[-1])
    assert info.asr, "STT Info advertises an asr program"
    languages = {
        lang for program in info.asr for model in program.models for lang in model.languages
    }
    assert "en" in languages


async def test_tts_plain_synthesize(tts_kokoro_service: LiveService) -> None:
    async with AsyncTcpClient(
        tts_kokoro_service.wyoming_host, tts_kokoro_service.wyoming_port
    ) as client:
        await client.write_event(
            Synthesize(
                text="Turn on the kitchen lights.", voice=SynthesizeVoice(name="af_heart")
            ).event()
        )
        events = await _read_until(client, AudioStop)
    types = [e.type for e in events]
    assert AudioStart.is_type(types[0])
    start = AudioStart.from_event(events[0])
    assert (start.rate, start.width, start.channels) == (TTS_SAMPLE_RATE, 2, 1)
    audio = b"".join(AudioChunk.from_event(e).audio for e in events if AudioChunk.is_type(e.type))
    assert len(audio) > MIN_AUDIO_BYTES, "no audio produced"


async def test_tts_streaming_speaks_the_text_once(
    tts_kokoro_service: LiveService, stt_service: LiveService
) -> None:
    """The streaming shape answers with one AudioStart, one AudioStop and one
    SynthesizeStopped, and the mid-stream compatibility ``Synthesize`` is
    ignored, not spoken. Round-trip the streamed audio through STT and assert
    the message is heard once (``kitchen`` appears a single time), which
    counting events cannot show."""
    async with AsyncTcpClient(
        tts_kokoro_service.wyoming_host, tts_kokoro_service.wyoming_port
    ) as client:
        await client.write_event(SynthesizeStart(voice=SynthesizeVoice(name="af_heart")).event())
        await client.write_event(SynthesizeChunk(text="Turn on the kitchen lights. ").event())
        await client.write_event(
            SynthesizeChunk(text="Set the thermostat to twenty degrees.").event()
        )
        # HA also sends a plain Synthesize with the whole message before stop; a
        # server that streams must ignore it or the text is spoken twice.
        await client.write_event(
            Synthesize(
                text="Turn on the kitchen lights. Set the thermostat to twenty degrees."
            ).event()
        )
        await client.write_event(SynthesizeStop().event())
        events = await _read_until(client, SynthesizeStopped)
    types = [e.type for e in events]
    assert sum(AudioStart.is_type(t) for t in types) == 1, "one AudioStart for the whole stream"
    assert sum(AudioStop.is_type(t) for t in types) == 1
    assert sum(SynthesizeStopped.is_type(t) for t in types) == 1
    audio = b"".join(AudioChunk.from_event(e).audio for e in events if AudioChunk.is_type(e.type))
    assert len(audio) > MIN_AUDIO_BYTES
    transcription = await asyncio.to_thread(
        stt_service.openai_client().audio.transcriptions.create,
        model="voiced",
        file=("stream.wav", pcm16_to_wav(audio, TTS_SAMPLE_RATE), "audio/wav"),
        language="en",
    )
    heard = transcription.text
    said = words(heard)
    assert said.count("kitchen") == 1, f"text spoken more than once: {heard!r}"
    assert EXPECTED_WORDS <= set(said), heard


async def test_tts_describe_returns_voices(tts_kokoro_service: LiveService) -> None:
    async with AsyncTcpClient(
        tts_kokoro_service.wyoming_host, tts_kokoro_service.wyoming_port
    ) as client:
        await client.write_event(Describe().event())
        events = await _read_until(client, Info)
    info = Info.from_event(events[-1])
    assert info.tts, "TTS Info advertises a tts program"
    voices = {voice.name for program in info.tts for voice in program.voices}
    assert "af_heart" in voices
