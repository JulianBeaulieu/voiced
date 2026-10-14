# API reference

voiced runs two HTTP servers, one per service. Each one also serves its
OpenAPI schema at `/docs`. The Wyoming listeners used by Home Assistant are
described at the [end of this page](#wyoming-protocol).

| Service | Default base URL | Endpoints |
|---|---|---|
| stt | `http://127.0.0.1:8881` | `/v1/audio/transcriptions`, `/v1/audio/transcriptions/stream`, `/v1/models`, `/health`, `/` |
| tts | `http://127.0.0.1:8880` | `/v1/audio/speech`, `/v1/audio/voices`, `/v1/models`, `/health`, `/` |

The `/v1` routes follow the OpenAI audio API closely enough that the official
clients work unmodified. Point the client at the service for the modality you
need; a client configured for the tts port cannot transcribe, and the other
way round. The `api_key` argument is required by the client library; any
value works unless you configured one (see [Authentication](#authentication)).

## Authentication

Off by default. Set `api_key` under `[server]` in the config (or
`VOICED_SERVER__API_KEY`) and every `/v1` route requires a bearer token.
`/health` and `/` never require one, so the watchdog and the status page keep
working.

```sh
curl -s http://127.0.0.1:8880/v1/models -H 'Authorization: Bearer <key>'
```

```python
from openai import OpenAI

tts = OpenAI(base_url="http://127.0.0.1:8880/v1", api_key="<key>")
print([m.id for m in tts.models.list()])
```

A missing or wrong token gets `401` with the standard
[error shape](#errors). The Wyoming listeners are not affected: the protocol
has no authentication.

## POST /v1/audio/transcriptions

Transcribe one complete audio file. Multipart form.

| Field | Required | Description |
|---|---|---|
| `file` | yes | Any format ffmpeg can decode (wav, mp3, m4a, flac, ogg, webm, ...). Decoded to mono 16 kHz before inference. Limited by `max_upload_mb` (default 50). |
| `model` | yes | Accepted for compatibility. The configured engine answers every request. |
| `language` | no | BCP-47 code such as `en`. Must be in the configured list; otherwise `400`. Omit to let the engine decide. |
| `response_format` | no | `json` (default), `text`, `verbose_json`, `srt`, `vtt`. |
| `prompt`, `temperature` | no | Accepted and ignored. |

<!-- TODO(phase2): verify how `model` is matched in the merged API -->

```sh
curl -s http://127.0.0.1:8881/v1/audio/transcriptions \
  -F file=@clip.wav -F model=parakeet
```

```json
{"text": "hello world"}
```

```sh
curl -s http://127.0.0.1:8881/v1/audio/transcriptions \
  -F file=@clip.wav -F model=parakeet -F response_format=verbose_json
```

```json
{
  "task": "transcribe",
  "language": "en",
  "duration": 2.4,
  "text": "hello world",
  "segments": [
    {"id": 0, "start": 0.0, "end": 2.4, "text": "hello world"}
  ]
}
```

`text` returns the plain transcript with `Content-Type: text/plain`; `srt`
and `vtt` return subtitle files built from the segments.

```python
from openai import OpenAI

stt = OpenAI(base_url="http://127.0.0.1:8881/v1", api_key="unused")

with open("clip.wav", "rb") as f:
    result = stt.audio.transcriptions.create(model="parakeet", file=f)
print(result.text)

with open("clip.wav", "rb") as f:
    verbose = stt.audio.transcriptions.create(
        model="parakeet", file=f, response_format="verbose_json"
    )
for segment in verbose.segments:
    print(segment.start, segment.end, segment.text)
```

Silence returns an empty `text`, not an error.

## WS /v1/audio/transcriptions/stream

Live transcription over a WebSocket. Send audio as it is captured, receive
partial hypotheses while speaking and a final transcript at the end. Built for
voice-agent frameworks such as Pipecat.

<!-- TODO(phase2): verify the message schema against the merged implementation -->

Connect to `ws://127.0.0.1:8881/v1/audio/transcriptions/stream`. With an API
key, send it as the `Authorization: Bearer <key>` header on the upgrade
request. The connection holds one inference slot for its whole life, so the
queue limit applies to concurrent streams.

### Messages from the client

| Type | Payload | Meaning |
|---|---|---|
| text frame | `{"type": "start", "language": "en", "sample_rate": 16000, "encoding": "pcm_s16le"}` | Must be the first message. `language` is optional. `sample_rate` and `encoding` describe the binary frames; the defaults shown are the only values guaranteed in v1. |
| binary frame | raw PCM | Audio chunks. Any size; 20 to 100 ms per frame keeps latency low. |
| text frame | `{"type": "stop"}` | End of speech. The server finalizes and closes after sending `final`. |

The server does not run end-of-speech detection on this endpoint. The client
decides when the utterance ends and sends `stop`. Use the Wyoming listener if
you want the caller to supply end-of-speech, or add a VAD on the client.

### Messages from the server

| Type | Payload | When |
|---|---|---|
| `partial` | `{"type": "partial", "text": "hello wor"}` | After audio arrives and the hypothesis changed. Text may be revised. |
| `final` | `{"type": "final", "text": "hello world", "language": "en", "duration": 2.4, "segments": [...]}` | Once, after `stop`. Same fields as `verbose_json`. |
| `error` | `{"type": "error", "error": {"message": "...", "type": "..."}}` | On any failure, followed by a close. `type` values match the [HTTP errors](#errors). |

```python
import asyncio
import json

import websockets


async def main() -> None:
    uri = "ws://127.0.0.1:8881/v1/audio/transcriptions/stream"
    async with websockets.connect(uri) as ws:
        await ws.send(json.dumps({"type": "start", "language": "en"}))
        with open("clip.pcm", "rb") as f:  # mono int16 16 kHz
            while chunk := f.read(3200):  # 100 ms
                await ws.send(chunk)
        await ws.send(json.dumps({"type": "stop"}))
        async for message in ws:
            event = json.loads(message)
            print(event["type"], event.get("text"))


asyncio.run(main())
```

## POST /v1/audio/speech

Synthesize text. JSON body.

| Field | Required | Description |
|---|---|---|
| `input` | yes | Text to speak. Limited by `max_input_chars` (default 4096). Numbers, times and acronyms are normalized before synthesis; markdown is stripped. |
| `model` | yes | Accepted for compatibility. The configured engine answers every request. |
| `voice` | no | A voice id from `/v1/audio/voices`. Defaults to `default_voice` from the config (`af_heart`). Unknown voices get `400`. |
| `speed` | no | Multiplier, default `1.0`. |
| `response_format` | no | `wav` (default), `pcm`, `mp3`, `opus`. |

<!-- TODO(phase2): verify how `model` is matched in the merged API -->

```sh
curl -s http://127.0.0.1:8880/v1/audio/speech \
  -H 'Content-Type: application/json' \
  -d '{"model": "kokoro", "input": "The meeting is at 8:14.", "voice": "af_heart"}' \
  -o out.wav
```

```python
from openai import OpenAI

tts = OpenAI(base_url="http://127.0.0.1:8880/v1", api_key="unused")
response = tts.audio.speech.create(
    model="kokoro", voice="af_heart", input="The meeting is at 8:14."
)
response.write_to_file("out.wav")
```

### Streaming

`pcm` streams: the response is `audio/pcm` sent chunked, mono int16
little-endian at 24 kHz, and the first bytes arrive as soon as the first
sentence is synthesized. Play it as it comes in:

```sh
curl -sN http://127.0.0.1:8880/v1/audio/speech \
  -H 'Content-Type: application/json' \
  -d '{"model": "kokoro", "input": "First sentence. Second sentence.", "response_format": "pcm"}' \
  | ffplay -f s16le -ar 24000 -ch_layout mono -nodisp -autoexit -
```

```python
from openai import OpenAI

tts = OpenAI(base_url="http://127.0.0.1:8880/v1", api_key="unused")
with tts.audio.speech.with_streaming_response.create(
    model="kokoro",
    voice="af_heart",
    input="First sentence. Second sentence.",
    response_format="pcm",
) as response:
    for chunk in response.iter_bytes(chunk_size=4800):
        play(chunk)  # your audio sink
```

`wav`, `mp3` and `opus` are encoded once the whole text is synthesized, so
they do not stream. The `Content-Type` is `audio/wav`, `audio/mpeg` or
`audio/ogg` respectively.

<!-- TODO(phase2): verify which formats stream in the merged API -->

## GET /v1/audio/voices

```sh
curl -s http://127.0.0.1:8880/v1/audio/voices
```

```json
{
  "voices": [
    {"id": "af_heart", "name": "Heart", "language": "en-US", "gender": "female"},
    {"id": "bf_emma", "name": "Emma", "language": "en-GB", "gender": "female"}
  ]
}
```

Only voices whose language is in the configured `languages` list are
returned. Kokoro voice ids start with a language and gender code: `af_` and
`am_` are American English female and male, `bf_` and `bm_` British.

## GET /v1/models

Available on both services. Lists the one model the service runs, plus the
engine name, its languages and the weights license.

```sh
curl -s http://127.0.0.1:8881/v1/models
```

```json
{
  "object": "list",
  "data": [
    {
      "id": "mlx-community/parakeet-tdt-0.6b-v3",
      "object": "model",
      "owned_by": "voiced",
      "engine": "parakeet",
      "task": "transcription",
      "languages": ["en"],
      "license": "CC-BY-4.0"
    }
  ]
}
```

```python
from openai import OpenAI

stt = OpenAI(base_url="http://127.0.0.1:8881/v1", api_key="unused")
for model in stt.models.list():
    print(model.id)
```

## GET /health

No authentication. Returns `200` once the model is loaded and warm, `503`
with `"status": "starting"` before that. The watchdog polls this.

```sh
curl -s http://127.0.0.1:8880/health
```

```json
{
  "status": "ok",
  "service": "tts",
  "engine": "kokoro",
  "model": "mlx-community/Kokoro-82M-bf16",
  "model_loaded": true,
  "uptime_s": 1234.5,
  "queue_depth": 0,
  "queue_capacity": 8,
  "last_inference_at": 1760450000.0,
  "last_inference_ms": 92.3,
  "last_error": null,
  "version": "0.0.1"
}
```

| Field | Meaning |
|---|---|
| `status` | `starting` until the model is loaded and warm, `degraded` if the most recent inference raised, `ok` otherwise. |
| `queue_depth`, `queue_capacity` | Requests waiting or running, and the limit from `queue_depth` in the config. |
| `last_inference_at` | Unix time of the last successful inference, `null` before the first. |
| `last_inference_ms` | Duration of that inference. |
| `last_error` | Exception name and message of the last failed inference, cleared by the next success. |

The schema is frozen because the watchdog and the status page depend on it.

## GET /

The status page: models, memory, p50 and p95 latency, recent errors, and a
test box to type text and hear it or upload audio and read the transcript.
HTML, no authentication.

<!-- TODO(phase2): verify against the merged status page -->

## Errors

Every error is JSON with one `error` object, in the shape OpenAI clients
expect:

```json
{"error": {"message": "unknown voice 'xx_nobody'", "type": "UnknownVoice"}}
```

`type` is a snake_case code for errors the server detects itself, and the
exception class name for errors an engine raised about the request.

<!-- TODO(phase2): verify the `type` casing convention and the full code list against the merged API -->

| Status | `type` | Cause |
|---|---|---|
| 400 | `UnknownVoice`, `UnsupportedLanguage`, `EngineError` | The request asked for something the engine cannot do. |
| 400 | `invalid_request` | Malformed body, undecodable audio, text over `max_input_chars`. |
| 401 | `unauthorized` | `api_key` is set and the bearer token is missing or wrong. |
| 413 | `payload_too_large` | Upload over `max_upload_mb`. |
| 422 | validation error | FastAPI's validation response for missing or mistyped fields. |
| 500 | `engine_error` | The engine failed. `/health` turns `degraded`; the watchdog may restart the service. |
| 503 | `server_busy` | The inference queue is full. The response carries `Retry-After: 1`. |
| 503 | `starting` | Only from `/health` while the model loads. |

The `openai` client raises `openai.BadRequestError`,
`openai.AuthenticationError`, `openai.InternalServerError` and
`openai.APIStatusError` (for 503) accordingly, with `error.message` in the
exception.

## Concurrency

Each service runs one inference at a time and queues the rest, up to
`queue_depth`; beyond that, requests fail at once with `503`. Streaming
requests hold their place for the whole stream. Wyoming requests share the
same queue. How the queue works is described in
[architecture.md](architecture.md#inference-queue).

## Wyoming protocol

The stt service listens on TCP 10300 and the tts service on TCP 10200
(`wyoming_port` in each section). The listeners share the loaded model and
the queue with the HTTP routes. Home Assistant setup is in
[home-assistant.md](home-assistant.md); this section is for other Wyoming
clients.

- STT: `Describe` answers `Info` with one `AsrProgram` named `voiced`, the
  model id and the configured languages. Send `Transcribe`, `AudioStart`,
  `AudioChunk` events, then `AudioStop`; the answer is `Transcript`. Any
  sample width, channel count and rate is accepted and converted to mono
  16 kHz. `requires_external_vad` is true: the client decides when speech
  ends.
- TTS: `Info` lists one `TtsProgram` with a `TtsVoice` per voice and
  `supports_synthesize_streaming` true. Plain `Synthesize` answers
  `AudioStart`, `AudioChunk` events, `AudioStop` at 24 kHz, 16-bit, mono.
  Streaming `SynthesizeStart`, `SynthesizeChunk`, `SynthesizeStop` synthesizes
  sentence by sentence as text arrives and ends with `AudioStop` then
  `SynthesizeStopped`.
- Errors come back as a Wyoming `Error` event with `code` set to
  `queue_full`, `engine_error` or the engine exception name.
- Discovery: with `bind` set to a non-loopback address and `zeroconf = true`
  (the default), `voiced-stt` and `voiced-tts` are advertised as
  `_wyoming._tcp.local.`.

The design notes and what Home Assistant's integration actually expects are
in ADR 0004, `docs/decisions/0004-wyoming.md`.

<!-- TODO(phase2): link the ADR once it is on main -->
