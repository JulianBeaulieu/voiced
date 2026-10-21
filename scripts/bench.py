#!/usr/bin/env python3
"""Benchmark a live voiced server over HTTP and WebSocket.

This is a standalone script rather than a ``voiced`` subcommand: benchmarking
is a developer tool run against servers someone else started, not an operator
command, and keeping it out of the CLI keeps its measurement code and the
``openai`` dependency off the daemon's path. See ADR 0015.

Start the servers first (``uv run voiced serve stt`` and ``uv run voiced serve
tts``, plus optionally more TTS servers running other engines on other
ports, each named with ``--extra-tts NAME=URL[,VOICE]``, voice ``default``
unless given), then point this at them (add ``--api-key`` or
``VOICED_API_KEY`` when the servers set ``server.api_key``)::

    uv run python scripts/bench.py \\
        --stt-url http://127.0.0.1:8881 --tts-url http://127.0.0.1:8880 \\
        --extra-tts Phonon=http://127.0.0.1:8860 \\
        --extra-tts OmniVoice=http://127.0.0.1:8850,en-US-female

It prints a Markdown table. What each row measures:

* first audio: from sending the request with the ``openai`` client (streamed,
  ``pcm``) to the first byte of audio, for the single sentence ``"Turn on the
  kitchen lights."``;
* streaming finalize: the clip streamed over the WebSocket in real time, then
  ``end``; from sending ``end`` to the ``final`` event. Measured with 200 ms of
  trailing digital silence before ``end`` (the endpointer's window, ADR 0008)
  and with 0 ms (a push-to-talk client that stops at the last word), each at
  ``--alignments`` lead offsets of 20 ms, for 64 ms and 100 ms chunks; then
  once more with each TTS server synthesizing continuously, so both models
  contend for the one GPU;
* streaming finalize with ``--streams`` clients at once: each streams the clip
  again and again (64 ms chunks, real time, a lead offset that steps 20 ms
  per utterance and a pause of up to 0.8 s between them), so each one's end
  of speech falls at every phase of the others' encoder calls (ADR 0020);
* first audio while another client speaks: the first-audio request, sent at
  a random point (0.2 to 0.6 s after the last reply) while a second client
  streams the clip over the WebSocket in real time, again and again, so
  synthesis can start during an STT encoder call, which goes first (ADR 0019).
  Three times ``--trials`` requests, since the worst case is the point;
* batch transcription: the ``--wav`` clip tiled to about 10 s, uploaded as WAV
  with the ``openai`` client, until the response;
* memory: read from each service's ``/metrics.json`` 1 s after a long reply
  (TTS) or a batch of 10 s clips (STT), once freed GPU memory has gone back to
  the system (it takes under 0.3 s): the physical footprint (what Activity
  Monitor and memory pressure count, Metal buffers included), RSS for
  reference, and MLX's active, cached and peak allocation. The MLX peak is the
  highest allocation since the service started, the transient a request (or,
  for Parakeet, the load) reaches;
* the speech-to-text to LLM to speech loop, only when ``LLM_BASE_URL`` and
  ``LLM_API_KEY`` are set: the clip streamed to STT, the transcript sent to an
  OpenAI-compatible chat endpoint (model ``LLM_MODEL`` or ``--llm-model``), the
  first clause of the streamed reply synthesized, until its first audio byte.
  No endpoint address is ever read from or written to the repository.

The script never starts or stops a server.

Acceptance targets: Kokoro first audio <= 200 ms, streaming finalize <= 150 ms
worst case (50 ms median), 10 s batch <= 300 ms, STT+TTS footprint <= 3 GB.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import random
import re
import statistics
import sys
import threading
import time
import wave
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import numpy as np
import websockets.sync.client as ws_sync
from numpy.typing import NDArray

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_WAV = REPO_ROOT / "tests" / "fixtures" / "speech_en.wav"
STT_SAMPLE_RATE = 16_000
TTS_SAMPLE_RATE = 24_000  # every engine serves 24 kHz (the audio convention)
SILENCE_WINDOW_S = 0.2  # the default stt.vad_silence_ms
STREAMS = 2  # the default stt.streams
RECV_TIMEOUT_S = 30.0
SETTLE_S = 1.0
MEMORY_TARGET_BYTES = 3_000_000_000

FIRST_AUDIO_TEXT = "Turn on the kitchen lights."
LONG_REPLY = (
    "Turn on the kitchen lights and set the thermostat to twenty degrees. "
    "What is the weather like tomorrow morning in the city? Please play some relaxing "
    "music in the living room and dim the bedroom lights to about fifty percent. "
    "Also lock the front door and arm the alarm before everyone goes to sleep tonight."
)
LOOP_FALLBACK = "Okay."

CLAUSE = re.compile(r"^(.*?[.,!?;:])(\s|$)", re.S)
LLM_SYSTEM = (
    "You are a home voice assistant. Reply in one short clause of under eight words, "
    "then at most one more short sentence. No markdown. You have no live data."
)


# -- audio helpers -----------------------------------------------------------


def load_wav_int16(path: Path) -> tuple[NDArray[np.int16], int]:
    with wave.open(str(path)) as wav:
        if wav.getnchannels() != 1 or wav.getsampwidth() != 2:
            raise SystemExit(f"{path} must be mono 16-bit PCM")
        rate = wav.getframerate()
        pcm = wav.readframes(wav.getnframes())
    return np.frombuffer(pcm, dtype="<i2").copy(), rate


def wav_container(samples: NDArray[np.int16], rate: int) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(samples.tobytes())
    return buffer.getvalue()


def percentiles(values: list[float]) -> tuple[float, float, float]:
    arr = np.array(values, dtype=float)
    return float(np.median(arr)), float(np.percentile(arr, 95)), float(np.max(arr))


# -- server probes -----------------------------------------------------------


def metrics(url: str) -> dict[str, Any]:
    data: dict[str, Any] = httpx.get(f"{url}/metrics.json", timeout=10).json()
    return data


def engine_name(url: str) -> str:
    name: str = metrics(url)["engine"]["name"]
    return name


def memory(url: str) -> dict[str, Any]:
    """The service's own memory report: ``rss_bytes``, ``footprint_bytes``
    and ``mlx`` (``active_bytes``, ``cache_bytes``, ``peak_bytes``)."""
    mem: dict[str, Any] = metrics(url)["metrics"]["memory"]
    return mem


# -- measurements ------------------------------------------------------------


def auth_headers(api_key: str) -> dict[str, str]:
    """The ``Authorization`` header ``server.api_key`` requires; none without a key."""
    return {"Authorization": f"Bearer {api_key}"} if api_key else {}


def openai_client(url: str, api_key: str) -> Any:
    from openai import OpenAI

    # The client insists on a key; a server without ``server.api_key`` ignores it.
    return OpenAI(base_url=f"{url}/v1", api_key=api_key or "bench", max_retries=0, timeout=60)


def first_audio_ms(client: Any, voice: str, text: str) -> float:
    """Request to the first PCM byte, streamed through the ``openai`` client."""
    started = time.perf_counter()
    with client.audio.speech.with_streaming_response.create(
        model="voiced", voice=voice, input=text, response_format="pcm"
    ) as response:
        for chunk in response.iter_bytes():
            if chunk:
                break
    return (time.perf_counter() - started) * 1000


def tts_first_audio_ms(url: str, api_key: str, voice: str, trials: int) -> list[float]:
    client = openai_client(url, api_key)
    first_audio_ms(client, voice, FIRST_AUDIO_TEXT)  # warm
    return [first_audio_ms(client, voice, FIRST_AUDIO_TEXT) for _ in range(trials)]


def synthesize(url: str, api_key: str, voice: str, text: str) -> tuple[bytes, float]:
    """The whole reply as PCM at :data:`TTS_SAMPLE_RATE`, and the seconds it took."""
    started = time.perf_counter()
    pcm = (
        openai_client(url, api_key)
        .audio.speech.create(model="voiced", voice=voice, input=text, response_format="pcm")
        .read()
    )
    return pcm, time.perf_counter() - started


def batch_transcription_ms(url: str, api_key: str, wav: bytes, trials: int) -> list[float]:
    client = openai_client(url, api_key)

    def once() -> float:
        started = time.perf_counter()
        client.audio.transcriptions.create(
            model="voiced", file=("clip.wav", wav, "audio/wav"), language="en"
        )
        return (time.perf_counter() - started) * 1000

    once()  # warm
    return [once() for _ in range(trials)]


def stream_and_finalize(
    ws_url: str, api_key: str, stream: bytes, rate: int, chunk_samples: int
) -> tuple[str, float]:
    """Send ``stream`` over the WebSocket in real time, then ``end``. Returns
    the final text and the ms from sending ``end`` to the ``final`` event."""
    chunk_bytes = chunk_samples * 2
    with ws_sync.connect(ws_url, additional_headers=auth_headers(api_key), max_size=None) as ws:
        ws.send(json.dumps({"type": "config", "sample_rate": rate, "language": "en"}))
        t0 = time.perf_counter()
        for offset in range(0, len(stream), chunk_bytes):
            ws.send(stream[offset : offset + chunk_bytes])
            played = (offset + chunk_bytes) / 2 / rate
            time.sleep(max(0.0, t0 + played - time.perf_counter()))
        end_sent = time.perf_counter()
        ws.send(json.dumps({"type": "end"}))
        while True:
            event = json.loads(ws.recv(timeout=RECV_TIMEOUT_S))
            if event.get("type") == "final":
                return str(event["text"]), (time.perf_counter() - end_sent) * 1000


def ws_finalize_ms(
    ws_url: str,
    api_key: str,
    samples: NDArray[np.int16],
    rate: int,
    chunk_samples: int,
    alignments: int,
    trailing_silence_s: float,
) -> list[float]:
    """Finalize latency at each of ``alignments`` lead offsets (20 ms steps),
    with ``trailing_silence_s`` of digital silence streamed before ``end``.

    With the endpointer's 200 ms window the engine's speculative final encode
    has already run, so finalize is a few ms; with 0 ms finalize must still
    encode the tail."""
    silence = np.zeros(round(trailing_silence_s * rate), dtype="<i2")
    latencies: list[float] = []
    for i in range(alignments):
        lead = np.zeros(round(i * 0.02 * rate), dtype="<i2")
        stream = np.concatenate([lead, samples, silence]).tobytes()
        latencies.append(stream_and_finalize(ws_url, api_key, stream, rate, chunk_samples)[1])
    return latencies


def concurrent_finalize_ms(
    ws_url: str,
    api_key: str,
    samples: NDArray[np.int16],
    rate: int,
    clients: int,
    utterances: int,
    trailing_silence_s: float,
) -> list[float]:
    """Finalize latency of ``clients`` WebSocket clients streaming at once,
    ``utterances`` each, with ``trailing_silence_s`` before each ``end``."""
    silence = np.zeros(round(trailing_silence_s * rate), dtype="<i2")
    latencies: list[float] = []
    lock = threading.Lock()

    def client(k: int) -> None:
        pauses = random.Random(k)
        time.sleep(0.37 * k)
        for i in range(utterances):
            lead = np.zeros(round(((i * clients + k) % 32) * 0.02 * rate), dtype="<i2")
            stream = np.concatenate([lead, samples, silence]).tobytes()
            ms = stream_and_finalize(ws_url, api_key, stream, rate, 1024)[1]
            with lock:
                latencies.append(ms)
            time.sleep(pauses.uniform(0.0, 0.8))

    threads = [threading.Thread(target=client, args=(k,), daemon=True) for k in range(clients)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return latencies


@contextmanager
def tts_busy(url: str, api_key: str, voice: str) -> Iterator[None]:
    """Keep the TTS server synthesizing back to back for the duration."""
    stop = threading.Event()

    def loop() -> None:
        client = openai_client(url, api_key)
        while not stop.is_set():
            with client.audio.speech.with_streaming_response.create(
                model="voiced", voice=voice, input=LONG_REPLY, response_format="pcm"
            ) as response:
                for _ in response.iter_bytes():
                    if stop.is_set():
                        break

    thread = threading.Thread(target=loop, daemon=True)
    thread.start()
    time.sleep(0.5)  # let the first synthesis reach the GPU
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=60)


def first_audio_while_streaming_ms(
    tts_url: str,
    ws_url: str,
    api_key: str,
    voice: str,
    samples: NDArray[np.int16],
    rate: int,
    trials: int,
) -> list[float]:
    """First audio while another client streams ``samples`` (plus 0.3 s of
    silence) to the STT in real time, back to back, for the duration."""
    stream = np.concatenate([samples, np.zeros(round(0.3 * rate), dtype="<i2")]).tobytes()
    stop = threading.Event()

    def talk() -> None:
        while not stop.is_set():
            stream_and_finalize(ws_url, api_key, stream, rate, 1024)

    client = openai_client(tts_url, api_key)
    first_audio_ms(client, voice, FIRST_AUDIO_TEXT)  # warm
    gaps = random.Random(0)
    thread = threading.Thread(target=talk, daemon=True)
    thread.start()
    time.sleep(1.0)  # let the first encoder calls start
    try:
        latencies = []
        for _ in range(trials):
            time.sleep(gaps.uniform(0.2, 0.6))
            latencies.append(first_audio_ms(client, voice, FIRST_AUDIO_TEXT))
        return latencies
    finally:
        stop.set()
        thread.join(timeout=60)


@dataclass
class Loop:
    stt: float
    llm: float
    tts: float
    total: float
    fallbacks: int
    runs: int


def roundtrip(
    stt_url: str,
    tts_url: str,
    api_key: str,
    voice: str,
    samples: NDArray[np.int16],
    rate: int,
    model: str,
    trials: int,
) -> Loop | None:
    base = os.environ.get("LLM_BASE_URL")
    key = os.environ.get("LLM_API_KEY")
    if not base or not key:
        return None
    from openai import OpenAI

    llm = OpenAI(base_url=base, api_key=key, max_retries=0, timeout=30)
    tts = openai_client(tts_url, api_key)
    ws_url = stream_url(stt_url)
    silence = np.zeros(round(SILENCE_WINDOW_S * rate), dtype="<i2")
    stream_bytes = np.concatenate([samples, silence]).tobytes()

    def once() -> tuple[dict[str, float], bool]:
        heard, finalize_ms = stream_and_finalize(ws_url, api_key, stream_bytes, rate, 1600)
        stt_done = time.perf_counter()
        end = stt_done - finalize_ms / 1000  # when ``end`` was sent: the loop starts there
        clause, buffer = "", ""
        reply = llm.chat.completions.create(
            model=model,
            stream=True,
            max_tokens=200,
            messages=[
                {"role": "system", "content": LLM_SYSTEM},
                {"role": "user", "content": heard},
            ],
        )
        for piece in reply:
            delta = piece.choices[0].delta.content if piece.choices else None
            if delta:
                buffer += delta
                match = CLAUSE.match(spoken_text(buffer))
                if match and len(match.group(1).split()) >= 2:
                    clause = match.group(1)
                    break
        clause = clause or spoken_text(buffer).strip()
        # Hidden thinking only, or a reply cut off by max_tokens: speak a short
        # fallback so the TTS leg is still timed, and count it.
        fallback = not clause
        llm_done = time.perf_counter()
        tts_ms = first_audio_ms(tts, voice, clause or LOOP_FALLBACK)
        audio_done = llm_done + tts_ms / 1000
        return {
            "stt": (stt_done - end) * 1000,
            "llm": (llm_done - stt_done) * 1000,
            "tts": tts_ms,
            "total": (audio_done - end) * 1000,
        }, fallback

    once()  # warm
    runs = [once() for _ in range(trials)]
    med = {k: statistics.median(r[k] for r, _ in runs) for k in ("stt", "llm", "tts", "total")}
    return Loop(**med, fallbacks=sum(f for _, f in runs), runs=len(runs))


_THINK = re.compile(r"<think>.*?(</think>|$)", re.S)


def spoken_text(text: str) -> str:
    """The reply without a reasoning model's ``<think>`` block. An unclosed
    block (still thinking) counts as empty, so its text is never spoken."""
    return _THINK.sub("", text)


def stream_url(http_url: str) -> str:
    """The streaming transcription WebSocket of the STT service at ``http_url``."""
    return "ws" + http_url[len("http") :] + "/v1/audio/transcriptions/stream"


# -- reporting ---------------------------------------------------------------


@dataclass
class Row:
    metric: str
    value: str
    target: str = ""


@dataclass(frozen=True)
class ExtraTTS:
    """A TTS server beside Kokoro's, from ``--extra-tts NAME=URL[,VOICE]``."""

    name: str
    url: str
    voice: str


def extra_tts(value: str) -> ExtraTTS:
    name, sep, rest = value.partition("=")
    url, _, voice = rest.partition(",")
    if not (name and sep and url):
        raise argparse.ArgumentTypeError(f"expected NAME=URL[,VOICE], got {value!r}")
    return ExtraTTS(name, url, voice or "default")


def combined_row(name: str, stt_mem: dict[str, Any], tts_mem: dict[str, Any]) -> Row:
    """The STT and TTS services' memory together, against the 3 GB budget."""
    field = "footprint_bytes" if stt_mem.get("footprint_bytes") is not None else "rss_bytes"
    return Row(
        f"STT + TTS ({name}) {field.removesuffix('_bytes')}, idle after use",
        f"{(stt_mem[field] + tts_mem[field]) / 1e9:.2f} GB",
        f"<= {MEMORY_TARGET_BYTES / 1e9:.0f} GB",
    )


def print_table(rows: list[Row], authenticated: bool) -> None:
    """A Markdown table the README and ADR can paste. Says whether requests
    carried an API key, never the key."""
    print("\n| Metric | Measured | Target |\n|---|---|---|")
    for r in rows:
        print(f"| {r.metric} | {r.value} | {r.target} |")
    print(f"\nauth: {'key' if authenticated else 'none'}")


def fmt_ms(values: list[float]) -> str:
    med, p95, worst = percentiles(values)
    return f"median {med:.0f} / p95 {p95:.0f} / worst {worst:.0f} ms (n={len(values)})"


def fmt_mem(mem: dict[str, Any]) -> str:
    parts = []
    if mem.get("footprint_bytes") is not None:
        parts.append(f"footprint {mem['footprint_bytes'] / 1e9:.2f} GB")
    parts.append(f"RSS {mem['rss_bytes'] / 1e9:.2f} GB")
    mlx = mem.get("mlx")
    if mlx:
        parts.append(
            f"MLX active {mlx['active_bytes'] / 1e6:.0f} MB, "
            f"cache {mlx['cache_bytes'] / 1e6:.0f} MB, "
            f"peak {mlx['peak_bytes'] / 1e6:.0f} MB"
        )
    return "; ".join(parts)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Benchmark live voiced servers; prints a Markdown table.",
        epilog="Environment: LLM_BASE_URL and LLM_API_KEY enable the STT -> LLM -> TTS loop "
        "row (any OpenAI-compatible chat endpoint); LLM_MODEL sets its model.",
    )
    parser.add_argument("--stt-url", required=True, help="STT server, e.g. http://127.0.0.1:8881")
    parser.add_argument(
        "--tts-url", required=True, help="Kokoro TTS server, e.g. http://127.0.0.1:8880"
    )
    parser.add_argument(
        "--extra-tts",
        type=extra_tts,
        action="append",
        default=[],
        metavar="NAME=URL[,VOICE]",
        help="another TTS server, measured like Kokoro's; repeatable "
        "(e.g. OmniVoice=http://127.0.0.1:8850,en-US-female; voice default: 'default')",
    )
    parser.add_argument(
        "--wav",
        type=Path,
        default=DEFAULT_WAV,
        help="speech clip, 16 kHz mono 16-bit WAV; streamed as is, and tiled to about "
        "10 s for the batch row (default: tests/fixtures/speech_en.wav)",
    )
    parser.add_argument(
        "--alignments",
        type=int,
        default=32,
        help="lead offsets (20 ms apart) per streaming finalize row (default: 32)",
    )
    parser.add_argument(
        "--streams",
        type=int,
        default=STREAMS,
        help="clients at once for the concurrent finalize rows; at most the server's "
        f"stt.streams (default: {STREAMS}, its default)",
    )
    parser.add_argument(
        "--trials",
        type=int,
        default=10,
        help="measured requests per first-audio and batch row, after one warm-up (default: 10)",
    )
    parser.add_argument(
        "--llm-model",
        default=os.environ.get("LLM_MODEL", "default"),
        help="chat model for the loop row (default: $LLM_MODEL, else 'default')",
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("VOICED_API_KEY", ""),
        help="bearer key for servers that set server.api_key; empty sends none "
        "(default: $VOICED_API_KEY). Not $VOICED_SERVER__API_KEY: that variable "
        "configures a server started from the same shell, and the bench should not "
        "pick up a key meant for a local server when pointed at a remote one",
    )
    args = parser.parse_args(argv)

    samples, rate = load_wav_int16(args.wav)
    if rate != STT_SAMPLE_RATE:
        raise SystemExit(f"--wav must be {STT_SAMPLE_RATE} Hz, got {rate}")
    ten_second = np.tile(samples, max(1, round(10 * rate / len(samples))))
    rows: list[Row] = []
    kokoro_voice = "af_heart"
    key: str = args.api_key

    print(f"stt engine: {engine_name(args.stt_url)}, tts engine: {engine_name(args.tts_url)}")

    first = tts_first_audio_ms(args.tts_url, key, kokoro_voice, args.trials)
    rows.append(Row("Kokoro first audio", fmt_ms(first), "<= 200 ms"))
    extras: list[ExtraTTS] = args.extra_tts
    for x in extras:
        ms = tts_first_audio_ms(x.url, key, x.voice, args.trials)
        rows.append(Row(f"{x.name} first audio", fmt_ms(ms), "<= 200 ms"))

    ws_url = stream_url(args.stt_url)
    tails = ((SILENCE_WINDOW_S, "200 ms trailing silence"), (0.0, "0 ms trailing silence"))
    for chunk, label in ((1024, "64 ms"), (1600, "100 ms")):
        for trailing, tail in tails:
            fin = ws_finalize_ms(ws_url, key, samples, rate, chunk, args.alignments, trailing)
            rows.append(
                Row(f"Streaming finalize, {label} chunks, {tail}", fmt_ms(fin), "<= 150 ms worst")
            )
    with tts_busy(args.tts_url, key, kokoro_voice):
        for trailing, tail in tails:
            fin = ws_finalize_ms(ws_url, key, samples, rate, 1024, args.alignments // 2, trailing)
            rows.append(
                Row(
                    f"Streaming finalize, 64 ms chunks, {tail}, Kokoro synthesizing",
                    fmt_ms(fin),
                    "<= 150 ms worst",
                )
            )
    for x in extras:
        with tts_busy(x.url, key, x.voice):
            for trailing, tail in tails:
                fin = ws_finalize_ms(
                    ws_url, key, samples, rate, 1024, args.alignments // 2, trailing
                )
                rows.append(
                    Row(
                        f"Streaming finalize, 64 ms chunks, {tail}, {x.name} synthesizing",
                        fmt_ms(fin),
                        "<= 150 ms worst",
                    )
                )
    if args.streams > 1:
        for trailing, tail in tails:
            fin = concurrent_finalize_ms(
                ws_url, key, samples, rate, args.streams, args.alignments, trailing
            )
            rows.append(
                Row(
                    f"Streaming finalize, {args.streams} clients at once, 64 ms chunks, {tail}",
                    fmt_ms(fin),
                    "<= 150 ms worst",
                )
            )
    speaking = first_audio_while_streaming_ms(
        args.tts_url, ws_url, key, kokoro_voice, samples, rate, 3 * args.trials
    )
    rows.append(
        Row("Kokoro first audio while another client streams to STT", fmt_ms(speaking), "<= 200 ms")
    )
    for x in extras:
        speaking = first_audio_while_streaming_ms(
            x.url, ws_url, key, x.voice, samples, rate, 3 * args.trials
        )
        rows.append(
            Row(
                f"{x.name} first audio while another client streams to STT",
                fmt_ms(speaking),
                "<= 200 ms",
            )
        )

    ten = batch_transcription_ms(args.stt_url, key, wav_container(ten_second, rate), args.trials)
    rows.append(
        Row(f"Batch transcription, {len(ten_second) / rate:.1f} s clip", fmt_ms(ten), "<= 300 ms")
    )

    # Memory after use, not just after warm-up: a long reply on each TTS server
    # (the batch row above has already exercised STT).
    synthesize(args.tts_url, key, kokoro_voice, LONG_REPLY)
    time.sleep(SETTLE_S)
    stt_mem, tts_mem = memory(args.stt_url), memory(args.tts_url)
    rows.append(Row("STT memory", fmt_mem(stt_mem)))
    rows.append(Row("TTS (Kokoro) memory", fmt_mem(tts_mem)))
    combined = [combined_row("Kokoro", stt_mem, tts_mem)]
    for x in extras:
        synthesize(x.url, key, x.voice, LONG_REPLY)
        time.sleep(SETTLE_S)
        extra_mem = memory(x.url)
        rows.append(Row(f"TTS ({x.name}) memory", fmt_mem(extra_mem)))
        combined.append(combined_row(x.name, stt_mem, extra_mem))
    rows.extend(combined)

    loop = roundtrip(
        args.stt_url,
        args.tts_url,
        key,
        kokoro_voice,
        samples,
        rate,
        args.llm_model,
        max(3, args.trials // 3),
    )
    if loop is None:
        rows.append(Row("STT -> LLM -> TTS loop", "skipped (LLM_BASE_URL/LLM_API_KEY unset)"))
    else:
        note = (
            f"; {loop.fallbacks} of {loop.runs} replies had no spoken text and used "
            f"the {LOOP_FALLBACK!r} fallback"
            if loop.fallbacks
            else ""
        )
        rows.append(
            Row(
                "STT -> LLM -> TTS loop, end of speech to first audio (median)",
                f"STT {loop.stt:.0f} + LLM {loop.llm:.0f} + TTS {loop.tts:.0f} "
                f"= {loop.total:.0f} ms{note}",
            )
        )

    print_table(rows, authenticated=bool(key))
    return 0


if __name__ == "__main__":
    sys.exit(main())
