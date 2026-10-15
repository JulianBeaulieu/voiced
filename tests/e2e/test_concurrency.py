"""Concurrency against the live services.

Several simultaneous clients, the busy response when the queue is full (HTTP
batch, HTTP streamed speech, Wyoming), a client that hangs up mid-synthesis,
the two socket bounds that free the slot (``session_idle_s`` on a silent
streaming client, ``write_timeout_s`` on one that stops reading), and the STT
finalize and idle bounds while Kokoro keeps the GPU busy in the same process.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import httpx
import pytest
import websockets
from wyoming.client import AsyncTcpClient
from wyoming.error import Error
from wyoming.tts import Synthesize, SynthesizeVoice

from tests.e2e.conftest import (
    EXPECTED_WORDS,
    LONG_INPUT,
    MIN_AUDIO_BYTES,
    SILENCE_WINDOW_S,
    LiveService,
    read_wav_pcm16,
    wav_bytes,
)
from tests.e2e.test_ws_streaming import FINALIZE_WORST_S, collect_stream
from tests.stt.conftest import words
from voiced.engines.base import STT_SAMPLE_RATE

pytestmark = pytest.mark.model


def _speech_disconnects(service: LiveService) -> int:
    ops = service.metrics()["metrics"]["operations"]
    return ops.get("speech", {}).get("counts", {}).get("disconnect", 0)


def _wait(predicate: Callable[[], bool], timeout_s: float = 10.0, interval: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


async def _await_until(
    predicate: Callable[[], bool], timeout_s: float = 10.0, interval: float = 0.02
) -> bool:
    """:func:`_wait` for async tests: polls without blocking the event loop."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if await asyncio.to_thread(predicate):
            return True
        await asyncio.sleep(interval)
    return False


@contextmanager
def _holding_the_tts_slot(service: LiveService) -> Iterator[None]:
    """Hold the TTS service's only slot for the duration: a long synthesis
    (18 sentences, several seconds of GPU work) that has produced its first
    chunk. The slot is held while the engine runs, whatever the client reads,
    so the body runs while that synthesis is still in progress."""
    with httpx.stream(
        "POST",
        f"{service.v1_url}/audio/speech",
        json={
            "model": "voiced",
            "input": LONG_INPUT,
            "voice": "af_heart",
            "response_format": "pcm",
        },
        timeout=30,
    ) as response:
        assert response.status_code == 200
        next(response.iter_bytes())
        assert _wait(lambda: service.health()["queue_depth"] >= 1), "slot never taken"
        yield
        assert service.health()["queue_depth"] >= 1, "the long synthesis ended too early"


# --- several simultaneous clients -------------------------------------------


def test_simultaneous_transcriptions(stt_service: LiveService) -> None:
    wav = wav_bytes("speech_en.wav")

    def one() -> str:
        client = stt_service.openai_client()
        result = client.audio.transcriptions.create(
            model="voiced", file=("speech_en.wav", wav, "audio/wav"), language="en"
        )
        return result.text

    with ThreadPoolExecutor(max_workers=6) as pool:
        texts = list(pool.map(lambda _: one(), range(6)))
    assert len(texts) == 6
    assert stt_service.health()["queue_depth"] == 0
    for text in texts:
        assert EXPECTED_WORDS <= set(words(text)), text


def test_simultaneous_synthesis(tts_kokoro_service: LiveService) -> None:
    def one() -> int:
        client = tts_kokoro_service.openai_client()
        return len(
            client.audio.speech.create(
                model="voiced", voice="af_heart", input="Turn on the kitchen lights."
            ).read()
        )

    with ThreadPoolExecutor(max_workers=6) as pool:
        sizes = list(pool.map(lambda _: one(), range(6)))
    assert all(size > MIN_AUDIO_BYTES for size in sizes)


# --- the queue-full (busy) response -----------------------------------------


async def test_busy_response_when_queue_is_full(stt_service: LiveService) -> None:
    """With capacity 1, a held streaming slot makes a second request 503
    ``server_busy`` with a Retry-After header, not a 500 or a hang."""
    samples, rate = read_wav_pcm16("speech_en.wav")
    with stt_service.override(queue_capacity=1):
        async with websockets.connect(stt_service.ws_url, max_size=None) as ws:
            await ws.send(json.dumps({"type": "config", "sample_rate": rate, "language": "en"}))
            await ws.send(samples[: rate // 2].tobytes())  # start the session: slot held
            assert await _await_until(lambda: stt_service.health()["queue_depth"] >= 1), (
                "slot never taken"
            )

            response = await asyncio.to_thread(
                httpx.post,
                f"{stt_service.v1_url}/audio/transcriptions",
                files={"file": ("speech_en.wav", wav_bytes("speech_en.wav"), "audio/wav")},
                data={"language": "en"},
                timeout=30,
            )
    assert response.status_code == 503
    assert response.headers.get("Retry-After") == "1"
    assert response.json()["error"]["type"] == "server_busy"
    # The slot is released once the stream closed; the service still works.
    assert await _await_until(lambda: stt_service.health()["queue_depth"] == 0)


def test_streamed_speech_is_busy_before_any_audio(tts_kokoro_service: LiveService) -> None:
    """A streamed synthesis that finds the queue full gets the 503
    ``server_busy`` status itself, not a 200 followed by a cut stream (ADR 0005:
    ``QueueFull`` surfaces before the first byte)."""
    with tts_kokoro_service.override(queue_capacity=1), _holding_the_tts_slot(tts_kokoro_service):
        with httpx.stream(
            "POST",
            f"{tts_kokoro_service.v1_url}/audio/speech",
            json={
                "model": "voiced",
                "input": "Turn on the kitchen lights.",
                "voice": "af_heart",
                "response_format": "pcm",
            },
            timeout=30,
        ) as busy:
            body = busy.read()
    assert busy.status_code == 503, body[:200]
    assert busy.headers.get("Retry-After") == "1"
    assert json.loads(body)["error"]["type"] == "server_busy"
    assert _wait(lambda: tts_kokoro_service.health()["queue_depth"] == 0)


async def test_wyoming_synthesize_is_server_busy_when_the_queue_is_full(
    tts_kokoro_service: LiveService,
) -> None:
    """Wyoming reports a full queue as ``Error(code="server_busy")`` (ADR 0011)."""
    with tts_kokoro_service.override(queue_capacity=1):
        with _holding_the_tts_slot(tts_kokoro_service):
            async with AsyncTcpClient(
                tts_kokoro_service.wyoming_host, tts_kokoro_service.wyoming_port
            ) as client:
                await client.write_event(
                    Synthesize(
                        text="Turn on the kitchen lights.",
                        voice=SynthesizeVoice(name="af_heart"),
                    ).event()
                )
                async with asyncio.timeout(30):
                    while True:
                        event = await client.read_event()
                        assert event is not None, "connection closed without an Error"
                        if Error.is_type(event.type):
                            break
    assert Error.from_event(event).code == "server_busy"


# --- client hangs up mid-synthesis ------------------------------------------


def test_client_hangs_up_mid_synthesis(tts_kokoro_service: LiveService) -> None:
    before = _speech_disconnects(tts_kokoro_service)
    with httpx.stream(
        "POST",
        f"{tts_kokoro_service.v1_url}/audio/speech",
        json={
            "model": "voiced",
            "input": LONG_INPUT,
            "voice": "af_heart",
            "response_format": "pcm",
        },
        timeout=30,
    ) as response:
        assert response.status_code == 200
        for _ in response.iter_bytes(chunk_size=4096):
            break  # read one chunk, then drop the connection mid-stream

    assert _wait(lambda: _speech_disconnects(tts_kokoro_service) > before), (
        "disconnect not recorded"
    )
    # The service stays healthy, the slot is free, and it serves the next request.
    assert _wait(lambda: tts_kokoro_service.health()["queue_depth"] == 0)
    assert tts_kokoro_service.health()["status"] == "ok"
    ok = (
        tts_kokoro_service.openai_client()
        .audio.speech.create(model="voiced", voice="af_heart", input="Still here.")
        .read()
    )
    assert len(ok) > MIN_AUDIO_BYTES


# --- the socket bounds that free the slot -----------------------------------


async def test_session_idle_timeout_closes_a_silent_stream(stt_service: LiveService) -> None:
    """A streaming client that sends nothing for ``session_idle_s`` is dropped
    with 1001 (going away), freeing the slot."""
    with stt_service.override(session_idle_s=0.3):
        async with websockets.connect(stt_service.ws_url, max_size=None) as ws:
            await ws.send(json.dumps({"type": "config", "sample_rate": STT_SAMPLE_RATE}))
            with pytest.raises(websockets.ConnectionClosed) as excinfo:
                await asyncio.wait_for(ws.recv(), timeout=5.0)
    assert excinfo.value.rcvd is not None
    assert excinfo.value.rcvd.code == 1001
    assert await _await_until(lambda: stt_service.health()["queue_depth"] == 0)


def test_write_timeout_frees_a_non_reading_client(tts_kokoro_service: LiveService) -> None:
    """A synthesis client that stops reading is dropped after
    ``write_timeout_s`` (the send buffer fills), recorded as a disconnect, with
    the slot freed. The disconnect is observed while the client still holds the
    socket open, proving the server timed the write out on its own."""
    before = _speech_disconnects(tts_kokoro_service)
    with tts_kokoro_service.override(write_timeout_s=0.5):
        with httpx.stream(
            "POST",
            f"{tts_kokoro_service.v1_url}/audio/speech",
            json={
                "model": "voiced",
                "input": LONG_INPUT,
                "voice": "af_heart",
                "response_format": "pcm",
            },
            timeout=30,
        ) as response:
            assert response.status_code == 200
            # Never read the body; the server's send buffer fills and the
            # bounded write times out while we hold the connection open.
            timed_out = _wait(
                lambda: _speech_disconnects(tts_kokoro_service) > before, timeout_s=8.0
            )
    assert timed_out, "write timeout did not fire while the client held the socket"
    assert _wait(lambda: tts_kokoro_service.health()["queue_depth"] == 0)
    assert tts_kokoro_service.health()["status"] == "ok"


# --- STT bounds while Kokoro keeps the GPU busy -----------------------------


CONTENDED_PTT_CEILING_S = 0.4


@contextmanager
def _kokoro_synthesizing(service: LiveService) -> Iterator[Callable[[], int]]:
    """Keep Kokoro synthesizing back to back in a background thread. Yields a
    callable returning how many bytes of audio it has produced so far."""
    stop = threading.Event()
    produced = [0]
    errors: list[BaseException] = []

    def loop() -> None:
        try:
            while not stop.is_set():
                with httpx.stream(
                    "POST",
                    f"{service.v1_url}/audio/speech",
                    json={
                        "model": "voiced",
                        "input": LONG_INPUT,
                        "voice": "af_heart",
                        "response_format": "pcm",
                    },
                    timeout=60,
                ) as response:
                    for chunk in response.iter_bytes():
                        produced[0] += len(chunk)
                        if stop.is_set():
                            break
        except BaseException as exc:  # reported in the test, not lost in the thread
            errors.append(exc)

    thread = threading.Thread(target=loop, name="kokoro-load", daemon=True)
    thread.start()
    try:
        assert _wait(lambda: produced[0] > 0, timeout_s=30), "Kokoro never started"
        yield lambda: produced[0]
    finally:
        stop.set()
        thread.join(timeout=60)
    assert not errors, errors


async def test_stt_bounds_hold_while_kokoro_synthesizes(
    stt_service: LiveService, tts_kokoro_service: LiveService
) -> None:
    """With Kokoro synthesizing continuously on the same GPU, streaming
    finalize keeps its words and, with 200 ms of trailing silence, its 150 ms
    bound (64 and 100 ms chunks); with 0 ms it is held to a regression ceiling, and the idle bound
    still frees the slot. Both services share one process here, as the e2e tier
    runs them; the deployed services are two processes on the same GPU."""
    with _kokoro_synthesizing(tts_kokoro_service) as produced:
        before = produced()
        runs = [
            await collect_stream(
                stt_service.ws_url, chunk_samples=chunk, trailing_silence_s=trailing
            )
            for chunk in (1024, 1600)
            for trailing in (SILENCE_WINDOW_S, 0.0)
        ]
        with stt_service.override(session_idle_s=0.3):
            async with websockets.connect(stt_service.ws_url, max_size=None) as ws:
                await ws.send(json.dumps({"type": "config", "sample_rate": STT_SAMPLE_RATE}))
                with pytest.raises(websockets.ConnectionClosed) as excinfo:
                    await asyncio.wait_for(ws.recv(), timeout=5.0)
        during = produced() - before
    print(
        "\nfinalize under contention: "
        + ", ".join(f"{r.finalize_s * 1000:.0f} ms" for r in runs)
        + f"; Kokoro produced {during / 48000:.1f} s of audio meanwhile"
    )
    # Kokoro really was synthesizing throughout: several seconds of audio.
    assert during > 48000 * 2
    for run, trailing in zip(runs, (SILENCE_WINDOW_S, 0.0) * 2, strict=True):
        assert EXPECTED_WORDS <= set(words(run.final["text"])), run.final["text"]
        # With the endpointer's trailing silence the final encode has already
        # run, so the bound holds. With 0 ms, finalize encodes the tail while
        # Kokoro's GPU work is queued ahead of it, and misses 150 ms (ADR 0015,
        # known misses); this ceiling only catches a regression beyond that.
        bound = FINALIZE_WORST_S if trailing else CONTENDED_PTT_CEILING_S
        assert run.finalize_s < bound, (
            f"finalize ({trailing * 1000:.0f} ms trailing) took {run.finalize_s * 1000:.0f} ms"
        )
    assert excinfo.value.rcvd is not None and excinfo.value.rcvd.code == 1001
    assert await _await_until(lambda: stt_service.health()["queue_depth"] == 0)
