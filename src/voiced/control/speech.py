"""One short line from the running tts service, for ``tts.speak`` (ADR 0013).

The menu bar app plays a line in a voice before the user chooses it. It does
not know the API key, so the watchdog asks: :class:`Speech` sends the
service the key it was started with, as the watchdog's probe does.

The requests are plain HTTP/1.0 on asyncio streams rather than urllib in a
thread, so cancelling the task closes the socket: the service sees the
hang-up and stops synthesizing at its next step, which is what happens when
the app stops a preview or goes away. The whole exchange has one deadline,
and the audio has a length cap, so the answer always fits one control line.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import wave
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

from voiced.watchdog import ServiceSpec

MAX_TEXT_CHARS = 60
"""Enough for any preview line; this is for hearing a voice, not synthesis."""
MAX_SECONDS = 10.0
"""Longer audio is refused: 10 s of PCM is 640 KB once in base64, well under
the 1 MiB line a client reads."""
TIMEOUT_S = 30.0
"""For the whole exchange: long enough for a model unloaded while idle, or a
busy queue, to come round. The app waits longer, so it sees this error."""
SPEECH_RATE = 24_000
"""The tts service's raw PCM rate (``voiced.engines.base.TTS_SAMPLE_RATE``,
which the watchdog does not import)."""
_ERROR_BODY_BYTES = 64 * 1024


class SpeechUnavailable(RuntimeError):
    """The tts service is not supervised here, or did not answer."""


class SpeechRefused(RuntimeError):
    """The tts service answered with an error, does not serve the voice, or
    the audio is too long."""


class Speech:
    """``tts.speak``: ``text`` in ``voice`` as a WAV file. ``spec`` gives the
    tts service as the watchdog last started it, None when not supervised."""

    def __init__(
        self,
        spec: Callable[[], ServiceSpec | None],
        *,
        timeout_s: float = TIMEOUT_S,
        max_seconds: float = MAX_SECONDS,
    ) -> None:
        self.spec = spec
        self.timeout_s = timeout_s
        self.max_seconds = max_seconds

    async def __call__(self, text: str, voice: str) -> bytes:
        spec = self.spec()
        if spec is None:
            raise SpeechUnavailable("tts is not supervised by this watchdog")
        try:
            async with asyncio.timeout(self.timeout_s):
                return wav(await self._speak(spec, text, voice))
        except TimeoutError:
            raise SpeechUnavailable(
                f"the tts service did not answer within {self.timeout_s:g}s"
            ) from None

    async def _speak(self, spec: ServiceSpec, text: str, voice: str) -> bytes:
        # The service would speak a voice it does not serve in its default
        # voice (ADR 0022); a preview of that would mislead, so ask first.
        served = _json(await _request(spec, "/v1/audio/voices"))
        ids = [v.get("id") for v in served.get("data", []) if isinstance(v, dict)]
        if voice not in ids:
            raise SpeechRefused(f"the running tts service does not serve the voice {voice!r}")
        body = json.dumps({"input": text, "voice": voice, "response_format": "pcm"}).encode()
        limit = int(self.max_seconds * SPEECH_RATE) * 2
        pcm = await _request(spec, "/v1/audio/speech", body, limit=limit)
        if len(pcm) > limit:
            raise SpeechRefused(f"the line is longer than {self.max_seconds:g} s at this speed")
        return pcm


async def _request(
    spec: ServiceSpec, path: str, body: bytes | None = None, *, limit: int | None = None
) -> bytes:
    """The body of a 200 answer, read up to ``limit`` + 1 bytes (the caller
    refuses more). :class:`SpeechUnavailable` when the service does not
    answer or breaks off, :class:`SpeechRefused` with its message when it
    answers with an error. Closing the socket on the way out, cancelled or
    not, is the hang-up."""
    url = urlsplit(spec.base_url)
    try:
        reader, writer = await asyncio.open_connection(url.hostname, url.port)
    except OSError as exc:
        raise SpeechUnavailable(f"the tts service is not answering: {exc}") from None
    try:
        head = [
            f"{'GET' if body is None else 'POST'} {path} HTTP/1.0",
            f"Host: {url.netloc}",
            "User-Agent: voiced-watchdog",
        ]
        if spec.api_key:
            head.append(f"Authorization: Bearer {spec.api_key}")
        if body is not None:
            head += ["Content-Type: application/json", f"Content-Length: {len(body)}"]
        writer.write("\r\n".join(head).encode() + b"\r\n\r\n" + (body or b""))
        await writer.drain()
        status = (await reader.readline()).split()
        code = int(status[1]) if len(status) > 1 and status[1].isdigit() else 0
        while (await reader.readline()).strip():
            pass  # headers: HTTP/1.0, so the body ends when the server closes
        cap = (limit if code == 200 and limit is not None else _ERROR_BODY_BYTES) + 1
        data = bytearray()
        while len(data) < cap and (chunk := await reader.read(cap - len(data))):
            data += chunk
    except OSError as exc:
        raise SpeechUnavailable(f"the tts service broke off: {exc}") from None
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
    if code == 0:
        raise SpeechUnavailable("the tts service sent no HTTP answer")
    if code != 200:
        error = _json(bytes(data)).get("error")
        message = error.get("message") if isinstance(error, dict) else None
        text = str(message or f"the tts service answered HTTP {code}")
        raise SpeechUnavailable(text) if code >= 500 else SpeechRefused(text)
    return bytes(data)


def _json(body: bytes) -> dict[str, Any]:
    try:
        value = json.loads(body.decode("utf-8", "replace"))
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def wav(pcm: bytes, rate: int = SPEECH_RATE) -> bytes:
    """Mono int16 PCM in a WAV file with its real length, which every player
    reads (the service's streamed WAV has an unknown length)."""
    out = io.BytesIO()
    with wave.open(out, "wb") as file:
        file.setnchannels(1)
        file.setsampwidth(2)
        file.setframerate(rate)
        file.writeframes(pcm)
    return out.getvalue()


async def no_speech(text: str, voice: str) -> bytes:
    """For running the operations without a watchdog (the CLI fallback)."""
    raise SpeechUnavailable("the watchdog is not running")
