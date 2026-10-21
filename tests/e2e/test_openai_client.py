"""The unmodified ``openai`` Python client against a live voiced server.

Covers both endpoints and every response format each advertises, synthesis
streamed and non-streamed, and the Kokoro, Soprano, Phonon and Pocket TTS engines.
"""

from __future__ import annotations

import re

import pytest

from tests.e2e.conftest import (
    EXPECTED_WORDS,
    MIN_AUDIO_BYTES,
    LiveService,
    wav_bytes,
)
from tests.stt.conftest import word_accuracy, words

pytestmark = pytest.mark.model

SENTENCE = "Turn on the kitchen lights, and set the thermostat to twenty degrees."


def _audio_signature_ok(fmt: str, data: bytes) -> bool:
    """A cheap check that the bytes are really in ``fmt``."""
    if fmt == "wav":
        return data[:4] == b"RIFF" and data[8:12] == b"WAVE"
    if fmt == "pcm":
        return len(data) > 0 and len(data) % 2 == 0
    if fmt == "opus":
        return data[:4] == b"OggS"  # Opus in an Ogg container, like OpenAI
    if fmt == "mp3":
        return data[:3] == b"ID3" or (data[0] == 0xFF and (data[1] & 0xE0) == 0xE0)
    raise AssertionError(fmt)


# --- transcription ----------------------------------------------------------


@pytest.mark.parametrize("fmt", ["json", "text", "verbose_json", "srt", "vtt"])
def test_transcription_every_response_format(stt_service: LiveService, fmt: str) -> None:
    client = stt_service.openai_client()
    result = client.audio.transcriptions.create(
        model="voiced",
        file=("speech_en.wav", wav_bytes("speech_en.wav"), "audio/wav"),
        language="en",
        response_format=fmt,  # type: ignore[arg-type]
    )
    text = result.text if fmt in ("json", "verbose_json") else result
    assert isinstance(text, str)
    assert EXPECTED_WORDS <= set(words(text)), f"{fmt}: {text!r}"
    if fmt == "srt":
        # A numbered cue with an "hh:mm:ss,mmm --> hh:mm:ss,mmm" timing line.
        assert re.search(r"^1\n\d\d:\d\d:\d\d,\d{3} --> \d\d:\d\d:\d\d,\d{3}$", text, re.M), text
    if fmt == "vtt":
        assert text.startswith("WEBVTT"), text
        assert re.search(r"^\d\d:\d\d:\d\d\.\d{3} --> \d\d:\d\d:\d\d\.\d{3}", text, re.M), text
    if fmt == "verbose_json":
        assert result.segments, "verbose_json carries timed segments"
        assert result.duration and result.duration > 0
        assert result.language


def test_transcription_from_a_file_object(stt_service: LiveService) -> None:
    """The exact call most tutorials show: a real file handle, default format."""
    from tests.e2e.conftest import FIXTURES

    client = stt_service.openai_client()
    with open(FIXTURES / "speech_en_trailing_silence.wav", "rb") as handle:
        result = client.audio.transcriptions.create(model="voiced", file=handle)
    assert {"weather", "tomorrow", "morning"} <= set(words(result.text)), result.text


# --- speech -----------------------------------------------------------------


@pytest.mark.parametrize("fmt", ["wav", "pcm", "mp3", "opus"])
def test_speech_kokoro_non_streamed(tts_kokoro_service: LiveService, fmt: str) -> None:
    client = tts_kokoro_service.openai_client()
    response = client.audio.speech.create(
        model="voiced",
        voice="af_heart",
        input=SENTENCE,
        response_format=fmt,  # type: ignore[arg-type]
    )
    data = response.read()
    assert _audio_signature_ok(fmt, data), f"{fmt}: {data[:16]!r}"
    assert len(data) > MIN_AUDIO_BYTES, f"{fmt}: only {len(data)} bytes"


@pytest.mark.parametrize("fmt", ["wav", "pcm", "mp3", "opus"])
def test_speech_kokoro_streamed(tts_kokoro_service: LiveService, fmt: str) -> None:
    client = tts_kokoro_service.openai_client()
    chunks: list[bytes] = []
    with client.audio.speech.with_streaming_response.create(
        model="voiced",
        voice="af_heart",
        input=SENTENCE,
        response_format=fmt,  # type: ignore[arg-type]
    ) as response:
        for chunk in response.iter_bytes(chunk_size=4096):
            if chunk:
                chunks.append(chunk)
    data = b"".join(chunks)
    assert chunks, "no audio streamed"
    assert _audio_signature_ok(fmt, data), f"{fmt}: {data[:16]!r}"
    assert len(data) > MIN_AUDIO_BYTES


def test_speech_soprano(tts_soprano_service: LiveService) -> None:
    client = tts_soprano_service.openai_client()
    non_streamed = client.audio.speech.create(
        model="voiced", voice="default", input=SENTENCE, response_format="wav"
    ).read()
    assert _audio_signature_ok("wav", non_streamed)
    assert len(non_streamed) > MIN_AUDIO_BYTES
    streamed: list[bytes] = []
    with client.audio.speech.with_streaming_response.create(
        model="voiced", voice="default", input=SENTENCE, response_format="pcm"
    ) as response:
        for chunk in response.iter_bytes(chunk_size=4096):
            if chunk:
                streamed.append(chunk)
    assert _audio_signature_ok("pcm", b"".join(streamed))
    assert len(b"".join(streamed)) > MIN_AUDIO_BYTES


def test_default_voice_and_an_unknown_voice(tts_kokoro_service: LiveService) -> None:
    from openai import BadRequestError

    client = tts_kokoro_service.openai_client()
    # No voice: falls back to the configured default (af_heart), still produces audio.
    data = client.audio.speech.create(model="voiced", voice="", input="Hello there.").read()
    assert _audio_signature_ok("wav", data)
    with pytest.raises(BadRequestError):
        client.audio.speech.create(model="voiced", voice="not_a_voice", input="Hello.").read()


# --- content round trips (TTS -> STT) ---------------------------------------
#
# The format checks above only prove bytes in the right container. These feed
# the synthesized audio back through the STT service and assert the words come
# out, so the audio is really speech (and, for mp3/opus, that the encode path
# and Soprano's 32 kHz-to-24 kHz resample survive a decode).

CONTENT_TYPES = {"wav": "audio/wav", "mp3": "audio/mpeg", "opus": "audio/ogg"}


def _transcribe(stt_service: LiveService, audio: bytes, fmt: str) -> str:
    result = stt_service.openai_client().audio.transcriptions.create(
        model="voiced", file=(f"speech.{fmt}", audio, CONTENT_TYPES[fmt]), language="en"
    )
    return result.text


def test_kokoro_audio_says_the_words(
    tts_kokoro_service: LiveService, stt_service: LiveService
) -> None:
    audio = (
        tts_kokoro_service.openai_client()
        .audio.speech.create(
            model="voiced", voice="af_heart", input=SENTENCE, response_format="wav"
        )
        .read()
    )
    heard = _transcribe(stt_service, audio, "wav")
    assert EXPECTED_WORDS <= set(words(heard)), heard


@pytest.mark.parametrize("fmt", ["wav", "mp3", "opus"])
def test_soprano_audio_says_the_words(
    tts_soprano_service: LiveService, stt_service: LiveService, fmt: str
) -> None:
    audio = (
        tts_soprano_service.openai_client()
        .audio.speech.create(
            model="voiced",
            voice="default",
            input=SENTENCE,
            response_format=fmt,  # type: ignore[arg-type]
        )
        .read()
    )
    heard = _transcribe(stt_service, audio, fmt)
    # Soprano can clip the last word; require the content, not a perfect match.
    assert word_accuracy(heard, SENTENCE) >= 0.6, heard
    assert {"kitchen", "thermostat"} <= set(words(heard)), heard


@pytest.mark.parametrize("voice", ["Easton", "Captain Silas Blackwater"])
def test_phonon_audio_says_the_words(
    tts_phonon_service: LiveService, stt_service: LiveService, voice: str
) -> None:
    """A preset by its catalogue name, spaces and all; streamed as it is decoded."""
    client = tts_phonon_service.openai_client()
    streamed: list[bytes] = []
    with client.audio.speech.with_streaming_response.create(
        model="voiced", voice=voice, input=SENTENCE, response_format="wav"
    ) as response:
        streamed += [chunk for chunk in response.iter_bytes(chunk_size=4096) if chunk]
    audio = b"".join(streamed)
    assert len(streamed) > 1 and _audio_signature_ok("wav", audio)
    heard = _transcribe(stt_service, audio, "wav")
    assert word_accuracy(heard, SENTENCE) >= 0.8, heard


@pytest.mark.parametrize("voice", ["alba", "javert"])
def test_pocket_audio_says_the_words(
    tts_pocket_service: LiveService, stt_service: LiveService, voice: str
) -> None:
    """Streamed frame by frame while the STT service shares the GPU."""
    client = tts_pocket_service.openai_client()
    streamed: list[bytes] = []
    with client.audio.speech.with_streaming_response.create(
        model="voiced", voice=voice, input=SENTENCE, response_format="wav"
    ) as response:
        streamed += [chunk for chunk in response.iter_bytes(chunk_size=4096) if chunk]
    audio = b"".join(streamed)
    assert len(streamed) > 1 and _audio_signature_ok("wav", audio)
    heard = _transcribe(stt_service, audio, "wav")
    assert word_accuracy(heard, SENTENCE) >= 0.8, heard


def test_chatterbox_audio_says_the_words(
    tts_chatterbox_service: LiveService, stt_service: LiveService
) -> None:
    """Its one voice, streamed in pieces as each sentence is rendered."""
    client = tts_chatterbox_service.openai_client()
    streamed: list[bytes] = []
    with client.audio.speech.with_streaming_response.create(
        model="voiced", voice="default", input=SENTENCE, response_format="wav"
    ) as response:
        streamed += [chunk for chunk in response.iter_bytes(chunk_size=4096) if chunk]
    audio = b"".join(streamed)
    assert len(streamed) > 1 and _audio_signature_ok("wav", audio)
    heard = _transcribe(stt_service, audio, "wav")
    assert word_accuracy(heard, SENTENCE) >= 0.8, heard


def test_qwen3_asr_subtitles_kokoro_audio(
    tts_kokoro_service: LiveService, stt_qwen3_service: LiveService
) -> None:
    """Sentence cues timed by the forced aligner, inside the audio."""
    text = f"{SENTENCE} What is the weather like tomorrow?"
    audio = (
        tts_kokoro_service.openai_client()
        .audio.speech.create(model="voiced", voice="af_heart", input=text, response_format="wav")
        .read()
    )
    srt = stt_qwen3_service.openai_client().audio.transcriptions.create(
        model="voiced", file=("speech.wav", audio, "audio/wav"), response_format="srt"
    )
    cues = [block.splitlines() for block in str(srt).strip().split("\n\n")]
    assert len(cues) == 2, srt
    assert word_accuracy(" ".join(cue[2] for cue in cues), text) >= 0.9, srt
    assert all(re.fullmatch(r"\d\d:\d\d:\d\d,\d{3} --> \d\d:\d\d:\d\d,\d{3}", c[1]) for c in cues)


# --- discovery --------------------------------------------------------------


def test_discovery_endpoints(stt_service: LiveService, tts_kokoro_service: LiveService) -> None:
    models = stt_service.get("/v1/models").json()
    assert models["data"][0]["engine"] == "parakeet"
    assert "en" in models["data"][0]["languages"]
    voices = tts_kokoro_service.get("/v1/audio/voices").json()
    ids = {v["id"] for v in voices["data"]}
    assert "af_heart" in ids
