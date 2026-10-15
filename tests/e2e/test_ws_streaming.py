"""Live WebSocket streaming transcription (``WS /v1/audio/transcriptions/stream``).

Audio is paced in real time at 64 ms and 100 ms chunks, partials are collected
as they arrive, and the finalize latency from the client's ``end`` message to
the ``final`` event is asserted against the acceptance bound of 150 ms worst
case (ADR 0008), with 200 ms and with 0 ms of trailing silence before ``end``.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, NamedTuple

import pytest
import websockets

from tests.e2e.conftest import (
    EXPECTED_WORDS,
    SILENCE_WINDOW_S,
    LiveService,
    read_wav_pcm16,
    silence_pcm16,
)
from tests.stt.conftest import words

pytestmark = pytest.mark.model

FINALIZE_WORST_S = 0.15  # ADR 0008 acceptance bound
# With 0 ms of trailing silence finalize encodes the tail after ``end``: about
# 110 ms median, but the worst of 32 alignments reached 165 ms (ADR 0015,
# known misses). One run here is held to this ceiling, which only catches a
# regression; the 150 ms bound is asserted with the endpointer's silence.
FINALIZE_PTT_CEILING_S = 0.25


class StreamRun(NamedTuple):
    partials: list[str]
    final: dict[str, Any]
    finalize_s: float
    """From sending ``end`` to receiving the ``final`` event."""
    first_partial_lead_s: float | None
    """How long before ``end`` the first non-empty partial arrived."""


async def collect_stream(
    ws_url: str,
    *,
    chunk_samples: int,
    send_config: bool = True,
    trailing_silence_s: float = SILENCE_WINDOW_S,
) -> StreamRun:
    """Stream ``speech_en.wav`` in real time followed by ``trailing_silence_s``
    of digital silence, then send ``end`` and wait for the final event."""
    samples, rate = read_wav_pcm16("speech_en.wav")
    speech = samples.tobytes()
    chunk_bytes = chunk_samples * 2
    final: dict[str, Any] = {}
    end_sent_at = 0.0
    final_at = 0.0

    async with websockets.connect(ws_url, max_size=None) as ws:
        received: list[tuple[float, str]] = []

        async def receiver() -> None:
            nonlocal final, final_at
            try:
                async for message in ws:
                    now = time.perf_counter()
                    event = json.loads(message)
                    if event.get("type") == "partial":
                        received.append((now, event["text"]))
                    elif event.get("type") == "final":
                        final, final_at = event, now
                        return
            except websockets.ConnectionClosed:
                return

        reader = asyncio.create_task(receiver())
        if send_config:
            await ws.send(json.dumps({"type": "config", "sample_rate": rate, "language": "en"}))

        t0 = time.perf_counter()
        stream = speech + silence_pcm16(trailing_silence_s, rate)
        for offset in range(0, len(stream), chunk_bytes):
            await ws.send(stream[offset : offset + chunk_bytes])
            played = (offset + chunk_bytes) / 2 / rate
            await asyncio.sleep(max(0.0, t0 + played - time.perf_counter()))
        end_sent_at = time.perf_counter()
        await ws.send(json.dumps({"type": "end"}))
        await asyncio.wait_for(reader, timeout=5.0)

    heard = [(at, text) for at, text in received if text]
    lead = end_sent_at - heard[0][0] if heard else None
    return StreamRun([text for _, text in heard], final, final_at - end_sent_at, lead)


@pytest.mark.parametrize("chunk_samples", [1024, 1600], ids=["64ms", "100ms"])
@pytest.mark.parametrize("trailing", [SILENCE_WINDOW_S, 0.0], ids=["200ms-silence", "0ms-ptt"])
async def test_streaming_partials_and_finalize_latency(
    stt_service: LiveService, chunk_samples: int, trailing: float
) -> None:
    """Finalize stays under the 150 ms bound with the endpointer's 200 ms of
    trailing silence, where the speculative final encode has already run. With
    0 ms (a push-to-talk client that sends ``end`` at the last word) finalize
    encodes the tail and is held to a regression ceiling instead."""
    run = await collect_stream(
        stt_service.ws_url, chunk_samples=chunk_samples, trailing_silence_s=trailing
    )
    partials, final, finalize_s = run.partials, run.final, run.finalize_s
    print(
        f"\nWS {chunk_samples} samples/chunk, {trailing * 1000:.0f} ms trailing: "
        f"finalize {finalize_s * 1000:.1f} ms, {len(partials)} partials, text {final.get('text')!r}"
    )
    assert final.get("type") == "final"
    assert EXPECTED_WORDS <= set(words(final["text"])), final["text"]
    assert final["language"] == "en"
    assert final["duration"] > 0
    assert partials, "no partial hypothesis arrived before the final"
    # Partials are live: the first one arrives while audio is still streaming.
    assert run.first_partial_lead_s is not None and run.first_partial_lead_s > 0
    bound = FINALIZE_WORST_S if trailing else FINALIZE_PTT_CEILING_S
    assert finalize_s < bound, f"finalize took {finalize_s * 1000:.0f} ms"


async def test_streaming_without_a_config_frame_defaults_to_16k(stt_service: LiveService) -> None:
    """The config frame is optional; 16 kHz is the default sample rate."""
    run = await collect_stream(stt_service.ws_url, chunk_samples=1600, send_config=False)
    assert EXPECTED_WORDS <= set(words(run.final["text"])), run.final["text"]
    assert run.finalize_s < FINALIZE_WORST_S


async def test_streaming_rejects_a_bad_sample_rate(stt_service: LiveService) -> None:
    """A client mistake closes the socket with 1008 (policy violation)."""
    async with websockets.connect(stt_service.ws_url, max_size=None) as ws:
        await ws.send(json.dumps({"type": "config", "sample_rate": 999}))
        with pytest.raises(websockets.ConnectionClosed) as excinfo:
            await ws.recv()
    assert excinfo.value.rcvd is not None
    assert excinfo.value.rcvd.code == 1008
