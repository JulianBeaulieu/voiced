# API reference

voiced runs two HTTP servers, one per service. Each one also serves
interactive Swagger UI at `/docs` and its OpenAPI schema at
`/openapi.json`. The Wyoming listeners used by Home Assistant are
described at the [end of this page](#wyoming-protocol).

| Service | Default base URL | Endpoints |
|---|---|---|
| stt | `http://127.0.0.1:8881` | `/v1/audio/transcriptions`, `/v1/audio/transcriptions/stream`, `/v1/models`, `/health`, `/`, `/metrics.json` |
| tts | `http://127.0.0.1:8880` | `/v1/audio/speech`, `/v1/audio/voices`, `/v1/models`, `/health`, `/`, `/metrics.json` |

The `/v1` routes follow the OpenAI audio API closely enough that the official
clients work unmodified. Point the client at the service for the modality you
need; a client configured for the tts port cannot transcribe, and the other
way round. The `api_key` argument is required by the client library; any
value works unless you configured one (see [Authentication](#authentication)).

While the model is still loading after the service starts, every `/v1`
route answers `503` `server_busy` with the message `EngineNotLoaded: model
is still loading` and `Retry-After: 1`. `/health` shows the same state as
`starting`. A model unloaded after `idle_unload_s` is different: the next
request waits while it loads again, a fraction of a second, and is answered
(ADR 0033). A service switched off (`enabled = false`) does not listen at
all.

## Authentication

Off by default. Set `api_key` under `[server]` in the config (or
`VOICED_SERVER__API_KEY`) and every `/v1` route requires a bearer token,
including the WebSocket handshake. `/health`, `/` and `/metrics.json` never
require one, so the watchdog and the status page keep working. With a key
set, `/health` and `/metrics.json` leave out each error's detail text, which
could echo what a client sent; they keep its time and exception name.

Without a key, the API also answers requests a web page makes from your
browser: a page can post a form to `http://127.0.0.1:8881` and have it
transcribed, though it cannot read the answer unless it also uses DNS
rebinding. Set a key if that matters to you. `/docs` (the interactive API
reference) is open as well and loads its scripts from a CDN in the browser
that opens it.

```sh
curl -s http://127.0.0.1:8880/v1/models -H 'Authorization: Bearer <key>'
```

```python
from openai import OpenAI

tts = OpenAI(base_url="http://127.0.0.1:8880/v1", api_key="<key>")
print([m.id for m in tts.models.list()])
```

A missing or wrong token gets `401` with type `auth_error` in the standard
[error shape](#errors); a WebSocket handshake gets HTTP `403`. The Wyoming
listeners are not affected: the protocol has no authentication.

## The `model` field

Each service runs exactly one model, so `model` in a request is ignored:
`whisper-1`, `tts-1` or any other name selects the running engine.
The one exception is a name that belongs to another engine installed on the
same machine (its engine name or model id). That is a `400`
`model_not_served`, because the caller clearly expected a different model.

## Languages

A service advertises the engine's languages that match the configured
`languages` list, compared on the primary subtag and reported in the
engine's more specific form. With `languages = ["en"]` and Kokoro, that is
`["en-US", "en-GB"]`. A `language` hint is resolved the same way: `en`,
`en-us` and `en_GB` are accepted; anything without a match is `400` with
`UnsupportedLanguage` in the message. Voices are filtered by the same rule.

## POST /v1/audio/transcriptions

Transcribe one complete audio file. Multipart form.

```sh
curl -s http://127.0.0.1:8881/v1/audio/transcriptions \
  -F file=@clip.wav -F model=whisper-1
```

```json
{"text": "hello world"}
```

| Field | Required | Description |
|---|---|---|
| `file` | yes | Any single audio file ffmpeg can decode (wav, mp3, m4a, flac, ogg, webm, ...). Decoded to mono 16 kHz float32. Limited to `stt.max_upload_mb` (default 50), and so is the decoded audio, read as 16-bit 16 kHz: about 26 minutes at the default. Formats that refer to other inputs (`ffconcat` lists, playlists) are refused. |
| `model` | no | See [The `model` field](#the-model-field). |
| `language` | no | Language hint; see [Languages](#languages). |
| `response_format` | no | `json` (default), `text`, `verbose_json`, `srt`, `vtt`. |
| `timestamp_granularities[]` | no | Accepted and ignored; `verbose_json` always has segment times. |

Other OpenAI fields such as `prompt` and `temperature` are ignored.

```sh
curl -s http://127.0.0.1:8881/v1/audio/transcriptions \
  -F file=@clip.wav -F response_format=verbose_json
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

`text` and `srt` return `text/plain`, `vtt` returns `text/vtt`, both
subtitle formats built from the segments. Silence returns an empty `text`,
not an error.

| Status | `type` | When |
|---|---|---|
| 400 | `invalid_audio` | ffmpeg could not decode the upload. |
| 400 | `invalid_request_error` | Missing `file`, bad `response_format`, unsupported language, malformed multipart. |
| 413 | `payload_too_large` | The upload is over `stt.max_upload_mb` (the body is cut off while it is read, before parsing), or it decodes to more audio than that many megabytes of 16-bit 16 kHz audio. |
| 503 | `server_busy` | `queue_depth` uploads are already being decoded, or the inference queue is full. |
| 500 | `ffmpeg_missing` | ffmpeg is not installed on the server. |

```python
from openai import OpenAI

stt = OpenAI(base_url="http://127.0.0.1:8881/v1", api_key="unused")

with open("clip.wav", "rb") as f:
    result = stt.audio.transcriptions.create(model="whisper-1", file=f)
print(result.text)

with open("clip.wav", "rb") as f:
    verbose = stt.audio.transcriptions.create(
        model="whisper-1", file=f, response_format="verbose_json"
    )
for segment in verbose.segments:
    print(segment.start, segment.end, segment.text)
```

## WS /v1/audio/transcriptions/stream

Live transcription over a WebSocket. Send audio as it is captured, receive
partial hypotheses while speaking and a final transcript at the end. Built for
voice-agent frameworks such as Pipecat.

Connect to `ws://127.0.0.1:8881/v1/audio/transcriptions/stream`. With an API
key, send `Authorization: Bearer <key>` on the upgrade request. A bad key, or
a model that is still loading, refuses the handshake with HTTP `403`.

### Messages from the client

| Frame | Payload | Meaning |
|---|---|---|
| text, optional, first | `{"type": "config", "sample_rate": 48000, "language": "en"}` | Both keys optional. `sample_rate` is the rate of the binary frames, 8000 to 192000, default 16000; other rates are resampled on the server. `language` is a hint as in [Languages](#languages). Must come before any audio. |
| binary | raw PCM | Mono int16 little-endian at the configured rate. Any frame size; a partial sample is carried into the next frame. |
| text | `{"type": "end"}` | End of the utterance. The server finalizes, sends `final` and closes. |

The server does not detect end of speech on this endpoint; the client
decides and sends `end`. Inside the engine, Parakeet runs the Silero speech
detector to skip silence and to start its last encoder call during a pause.
When the audio ends in a short silence, as it does when the client detects
the end of speech itself, `final` usually arrives within a few milliseconds
of `end`. A clip sent in one burst has no such pause, so the engine encodes
it after `end`, one call per 0.8 s of audio: 466 ms for a 3.4 s clip. The config key
`stt.vad_silence_ms` (default 200) is the silence window for server-side end
of speech, which neither this endpoint nor Wyoming uses yet; changing it has
no effect in this version.

Partial transcripts trail the speaker by about 0.7 s, because the engine
encodes audio in 0.8 s steps. A pause of about one second inside an
utterance can repeat a short word in the streamed transcript; batch
transcription of the same audio does not. See
[ADR 0008](decisions/0008-stt-engine-parakeet.md).

### Messages from the server

| Type | Payload | When |
|---|---|---|
| `partial` | `{"type": "partial", "text": "hello wor"}` | When the hypothesis changes, at most every 250 ms. Text may be revised. |
| `final` | `{"type": "final", "text": "hello world", "language": "en", "duration": 2.4, "segments": [{"start": 0.0, "end": 2.4, "text": "hello world"}]}` | Once, after `end`, followed by a normal close (1000). |

Errors are reported only as the close code, with `ExceptionName: message` as
the close reason:

| Close code | Meaning |
|---|---|
| 1000 | Normal end after `final`. |
| 1001 | The client sent nothing for `server.session_idle_s` (default 30), or did not accept a write within `server.write_timeout_s` (default 10). Treated as a disconnect. |
| 1008 | Client error: a text frame that is not a JSON object, an unknown `type`, `config` after audio, a bad `sample_rate`, an unsupported language, or a stream longer than a Wyoming utterance may be (see the bounds under [Wyoming protocol](#wyoming-protocol)). |
| 1011 | The engine failed. `/health` turns `degraded`. |
| 1013 | The inference queue is full; retry later. |

The connection holds one inference slot for its whole life. Up to
`stt.streams` sessions (default 2) are transcribed at once, WebSocket and
Wyoming together. Setting `stt.streams` above `queue_depth` is an error, and
the service opens no more than the engine can interleave (its `max_streams`: 6
for parakeet, 1 for nemotron or an engine that declares nothing). The next session waits
for one of them to end while its audio is held, and a batch transcription
waits until none is open (ADR 0020). `queue_depth` bounds the sessions open or
waiting. A session carries at most `stt.max_upload_mb` worth of 16-bit 16 kHz
audio (about 26 minutes at the default) and lasts at most that long plus
`server.session_idle_s`. One frame may be up to 16 MiB, uvicorn's limit.

```python
import asyncio
import json

import websockets


async def main() -> None:
    uri = "ws://127.0.0.1:8881/v1/audio/transcriptions/stream"
    async with websockets.connect(uri) as ws:
        await ws.send(json.dumps({"type": "config", "sample_rate": 16000}))
        with open("clip.pcm", "rb") as f:  # mono int16 16 kHz
            while chunk := f.read(3200):  # 100 ms
                await ws.send(chunk)
        await ws.send(json.dumps({"type": "end"}))
        async for message in ws:
            event = json.loads(message)
            print(event["type"], event.get("text"))


asyncio.run(main())
```

## POST /v1/audio/speech

Synthesize text. JSON body. Every format streams: the first bytes leave as
soon as the first sentence is synthesized.

```sh
curl -s http://127.0.0.1:8880/v1/audio/speech \
  -H 'Content-Type: application/json' \
  -d '{"model": "tts-1", "input": "The meeting is at 8:14.", "voice": "af_heart"}' \
  -o out.wav
```

| Field | Required | Description |
|---|---|---|
| `input` | yes | Text to speak, not empty, at most `tts.max_input_chars` (default 4096). Markdown is stripped before synthesis; for an English voice, numbers, times, units and acronyms are spelled out, and other languages keep them as written. |
| `model` | no | See [The `model` field](#the-model-field). |
| `voice` | no | An id from [`/v1/audio/voices`](#get-v1audiovoices). Defaults to `tts.default_voice` (`af_heart`). |
| `speed` | no | Multiplier from 0.25 to 4. A value of 0 or less, or above 4, fails validation; one between 0 and 0.25 is refused by the engine. Both are 400 `invalid_request_error`. Defaults to `tts.speed` (1.0). Soprano, Phonon, Pocket TTS, Chatterbox and Marvis have no rate control and ignore it. |
| `response_format` | no | `wav` (default), `pcm`, `mp3`, `opus`. |

| Format | `Content-Type` | Body |
|---|---|---|
| `wav` | `audio/wav` | 24 kHz 16-bit mono PCM behind a header whose size fields are `0xFFFFFFFF` (length unknown); players read to the end of the stream. |
| `pcm` | `audio/pcm` | Raw int16 little-endian mono at 24 kHz. |
| `mp3` | `audio/mpeg` | Encoded by ffmpeg while synthesis runs. |
| `opus` | `audio/ogg` | Opus in an Ogg container, encoded by ffmpeg while synthesis runs. |

Errors found before the first audio, listed below, are ordinary HTTP
errors. An engine failure later in a long text ends the stream early,
since the status line has already been sent:

| Status | `type` | When |
|---|---|---|
| 400 | `invalid_request_error` | Empty `input`, a bad field, or a voice when the service serves none in the configured languages (`UnknownVoice` in the message). Any other voice that is not served is spoken in `tts.default_voice` (or the first served voice) with a warning in the log, so an OpenAI client's `alloy` or Home Assistant's voice of the previous engine still gets audio. |
| 413 | `payload_too_large` | `input` is longer than `tts.max_input_chars`. |
| 500 | `ffmpeg_missing` | `mp3` or `opus` requested and ffmpeg is not installed. |
| 503 | `server_busy` | The inference queue is full, or the model is loading. |

A client that hangs up before or during synthesis, or while its request
waits in the queue, stops the synthesis at the engine's next generation
step and is counted as a `disconnect` on the status page. A hang-up after
synthesis finished counts as `ok`: the audio was produced and only its
delivery failed. A client that stays connected but stops reading is cut
off after `server.write_timeout_s` (default 10 s), which frees its encoder
and buffered audio and leaves an expected ERROR line in the log ([details](operations.md#logs)).

```python
from openai import OpenAI

tts = OpenAI(base_url="http://127.0.0.1:8880/v1", api_key="unused")
response = tts.audio.speech.create(model="tts-1", voice="af_heart", input="The meeting is at 8:14.")
response.write_to_file("out.wav")
```

Play `pcm` as it arrives:

```sh
curl -sN http://127.0.0.1:8880/v1/audio/speech \
  -H 'Content-Type: application/json' \
  -d '{"input": "First sentence. Second sentence.", "response_format": "pcm"}' \
  | ffplay -f s16le -ar 24000 -ch_layout mono -nodisp -autoexit -
```

```python
from openai import OpenAI

tts = OpenAI(base_url="http://127.0.0.1:8880/v1", api_key="unused")
with tts.audio.speech.with_streaming_response.create(
    model="tts-1",
    voice="af_heart",
    input="First sentence. Second sentence.",
    response_format="pcm",
) as response:
    for chunk in response.iter_bytes(chunk_size=4800):
        play(chunk)  # your audio sink
```

## GET /v1/audio/voices

```sh
curl -s http://127.0.0.1:8880/v1/audio/voices
```

```json
{
  "object": "list",
  "data": [
    {"object": "voice", "id": "af_heart", "name": "Heart", "language": "en-US", "gender": "female"},
    {"object": "voice", "id": "bf_emma", "name": "Emma", "language": "en-GB", "gender": "female"}
  ]
}
```

Only voices in a configured language are listed. Kokoro voice ids start with
a language and gender code: `af_` and `am_` are American English female and
male, `bf_` and `bm_` British. Version 1 serves the 28 English voices.
Soprano, when selected, has one voice, `default`.

Phonon, when selected, has `default` (the model's own voice) and Gradium's 37
English presets, each named as Gradium's catalogue names it, spaces
included. The language is the preset's accent, `en` for the character
voices:

```json
{
  "object": "list",
  "data": [
    {"object": "voice", "id": "default", "name": "Default", "language": "en", "gender": null},
    {"object": "voice", "id": "Easton", "name": "Easton", "language": "en-US", "gender": "male"},
    {"object": "voice", "id": "Maeve", "name": "Maeve", "language": "en-GB", "gender": "female"},
    {"object": "voice", "id": "The Snow Queen", "name": "The Snow Queen", "language": "en", "gender": null}
  ]
}
```

Pocket TTS, when selected, has six of Kyutai's voices, by their lowercase
names: `alba` (the default), `marius`, `javert`, `fantine`, `eponine` and
`azelma`. Kyutai's `cosette` and `jean` are non-commercial and not offered.
Their language is `en`:

```json
{
  "object": "list",
  "data": [
    {"object": "voice", "id": "alba", "name": "Alba", "language": "en", "gender": "female"},
    {"object": "voice", "id": "javert", "name": "Javert", "language": "en", "gender": "male"}
  ]
}
```

Chatterbox Turbo, when selected, has one voice, `default`, the one the
model ships with (`"language": "en"`). Text for it may carry tags such as
`[laugh]`, `[sigh]` or `[whispering]`, which it performs instead of reading:

```sh
curl -s http://127.0.0.1:8880/v1/audio/speech \
  -H 'Content-Type: application/json' \
  -d '{"input": "That is the best news all week. [laugh] Thank you.", "voice": "default"}' \
  -o out.wav
```

OmniVoice, when selected, has six voices designed from attributes, one per
language; each is served when its language is in `tts.languages`. With
`languages = ["en", "de", "apc", "es", "fr", "zh"]`:

```json
{
  "object": "list",
  "data": [
    {"object": "voice", "id": "en-US-female", "name": "Young adult", "language": "en-US", "gender": "female"},
    {"object": "voice", "id": "de-DE-male", "name": "Middle-aged", "language": "de-DE", "gender": "male"},
    {"object": "voice", "id": "apc-female", "name": "Young adult", "language": "apc", "gender": "female"},
    {"object": "voice", "id": "es-ES-male", "name": "Young adult", "language": "es-ES", "gender": "male"},
    {"object": "voice", "id": "fr-FR-female", "name": "Middle-aged", "language": "fr-FR", "gender": "female"},
    {"object": "voice", "id": "zh-CN-male", "name": "Young adult", "language": "zh-CN", "gender": "male"}
  ]
}
```

Its `/v1/models` entry lists the configured languages the model speaks
(646 tags on its model card, `apc` for Levantine Arabic) and the licence
`CC-BY-NC; codec: Boson Higgs Audio 2 Community License`.

Marvis, when selected, has four voices, each a reference clip the model
continues: `conversational_a` (the default) and `conversational_b` in
English, `conversational_fr` in French and `conversational_de` in German.
The French and German ones are listed only when `tts.languages` includes
`fr` or `de`:

```json
{
  "object": "list",
  "data": [
    {"object": "voice", "id": "conversational_a", "name": "Conversational A", "language": "en", "gender": "female"},
    {"object": "voice", "id": "conversational_fr", "name": "Conversational FR", "language": "fr", "gender": "male"}
  ]
}
```

## GET /v1/models

Available on both services. Lists the one model the service runs, with the
engine name, the [languages](#languages) it serves and the weights license.

```sh
curl -s http://127.0.0.1:8880/v1/models
```

```json
{
  "object": "list",
  "data": [
    {
      "id": "mlx-community/Kokoro-82M-bf16",
      "object": "model",
      "owned_by": "voiced",
      "engine": "kokoro",
      "task": "speech",
      "languages": ["en-US", "en-GB"],
      "license": "Apache-2.0"
    }
  ]
}
```

The stt service reports `"task": "transcription"`. `license` is the
configured model's: with Phonon it is `Apache-2.0` for the default
`gradium/phonon-31m-en` and `CC-BY-NC-4.0` (non-commercial) for
`gradium/phonon-40m-en`:

```json
{
  "object": "list",
  "data": [
    {
      "id": "gradium/phonon-40m-en",
      "object": "model",
      "owned_by": "voiced",
      "engine": "phonon",
      "task": "speech",
      "languages": ["en-US", "en-GB"],
      "license": "CC-BY-NC-4.0"
    }
  ]
}
```

With Moonshine the stt service lists, for example:

```json
{
  "object": "list",
  "data": [
    {
      "id": "moonshine-ai/moonshine-streaming-small",
      "object": "model",
      "owned_by": "voiced",
      "engine": "moonshine",
      "task": "transcription",
      "languages": ["en"],
      "license": "MIT"
    }
  ]
}
```

```python
from openai import OpenAI

tts = OpenAI(base_url="http://127.0.0.1:8880/v1", api_key="unused")
for model in tts.models.list():
    print(model.id)
```

## GET /health

No authentication. `200` once the model is loaded and warm, and while it
is unloaded on purpose (`idle`, `loading`), `503` with
`"status": "starting"` before the first load, or `"status": "missing_model"` when the
configured model is not downloaded (the services never download one; the
process stays up and `last_error` names the `voiced models download`
command); the body is the same document either way. The services bind their ports before loading the model, so a loading
service answers `starting` rather than refusing connections. The watchdog
polls this.

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
  "version": "0.3.0"
}
```

| Field | Meaning |
|---|---|
| `status` | `starting` until the model is loaded and warm, `missing_model` if the configured model is not in the cache, `degraded` if the most recent inference (or a load for one) failed on the server side, `idle` while the model is unloaded after `idle_unload_s` without a request, `loading` while it loads again for a request that waits, `ok` otherwise. A request to an `idle` service is answered after the load, not refused. Client errors, busy answers and disconnects never change it. `missing_model` is new in 0.2.0 (ADR 0022), `idle` and `loading` are new in the next release (ADR 0033); clients should treat a status they do not know as not ready, except that a `200` answer takes requests. |
| `model_loaded` | Whether the model is in memory now: false while `starting`, `idle` and `loading`. |
| `queue_depth`, `queue_capacity` | Requests waiting or running, and the limit from `queue_depth` in the config. |
| `last_inference_at` | Unix time of the last successful inference, `null` before the first. |
| `last_inference_ms` | Duration of that inference while it held the GPU. For streams this is the whole session; for HTTP speech it is the synthesis, not the download. |
| `last_error` | Exception name and message of the last failed inference, cleared by the next success. With `server.api_key` set only the exception name is shown, since the message can echo what a client sent; the full text is in the service log. |

<!-- TODO(release): name the version that brings idle and loading in the /health status row -->

The schema is frozen because the watchdog and the status page depend on it.

## GET / and GET /metrics.json

`/` is the status page: service, engine, model, license, health, uptime,
resident memory, queue depth, p50 and p95 latency, recent errors, and a test
box (type text and hear it on tts, upload audio and read the transcript on
stt). It is one self-contained HTML file and fetches nothing from the
internet. If an API key is set, type it into the page to use the test box.

With `[server] browser_settings = true` the page also shows a settings form
(engine, model, voice, speed, `bind`, API key, zeroconf). It is off by
default. When on, it always needs the API key as a bearer token: with no
key set it is refused (403), on loopback too, because a web page you open
can reach a loopback port (DNS rebinding). It reads from
`GET /settings.json` and saves with `POST /settings` (`application/json`),
which take and return the same settings as `settings.get` and
`settings.set` on the local control channel
([operations](operations.md#control-channel)). Changes are written to the
config file and apply when the service restarts. A change to a model that is
not downloaded is refused with 409 `model_not_downloaded`.

`/metrics.json` is the data behind it:

| Key | Contents |
|---|---|
| `service` | `stt` or `tts` |
| `engine` | `name`, `model`, `license`, `languages` |
| `health` | The `/health` document |
| `metrics` | `operations`: per operation (`transcribe`, `stream`, `speech`), counts by outcome (`ok`, `busy`, `client_error`, `disconnect`, `failure`) and p50 and p95 latency over the last 1000 successful inferences. `recent_errors`: the last 50 errors. `memory`: `rss_bytes` (resident set), `footprint_bytes` (physical footprint as Activity Monitor and memory pressure count it, which includes MLX's GPU buffers; `null` off macOS) and `mlx` (`active_bytes`, `cache_bytes`, `peak_bytes` from MLX's allocator; `null` without MLX). |
| `api_key_required` | Whether the `/v1` routes need a key |
| `default_voice` | The tts default voice, `null` on stt |
| `settings_form` | `enabled` and `refused` (the reason, or `null`) for the settings form, which always needs the API key |

Metrics live in memory and reset when the service restarts.

## Errors

Every HTTP error is JSON with one `error` object, in the shape OpenAI clients
parse:

```json
{"error": {"message": "UnknownVoice: voice 'xx_nobody' is not served; see /v1/audio/voices", "type": "invalid_request_error"}}
```

`type` is a snake_case code. Errors an engine raised about the request start
the message with the exception name (`UnknownVoice`, `UnsupportedLanguage`,
`EngineNotLoaded`).

| Status | `type` | Cause |
|---|---|---|
| 400 | `invalid_request_error` | Bad or missing fields (FastAPI validation errors are 400, not 422, as with OpenAI), unknown voice, unsupported language, malformed multipart. |
| 400 | `invalid_audio` | Undecodable upload. |
| 400 | `model_not_served` | `model` names another installed engine. |
| 401 | `auth_error` | `api_key` is set and the bearer token is missing or wrong. |
| 404, 405 | `invalid_request_error` | Unknown path or method. |
| 413 | `payload_too_large` | Upload over `stt.max_upload_mb`, or `input` over `tts.max_input_chars`. |
| 500 | `server_error` | The engine failed, or an unexpected error in the server. An engine failure turns `/health` `degraded`. |
| 500 | `ffmpeg_missing` | ffmpeg is needed and not installed. |
| 503 | `server_busy` | The inference queue is full, or the model is still loading. Carries `Retry-After: 1`. |
| 503 | `model_not_downloaded` | The configured model is not downloaded (`/health` says `missing_model`). No `Retry-After`: retrying does not help until it is downloaded and the service restarted. |

The `openai` client raises `openai.BadRequestError` for 400,
`openai.AuthenticationError` for 401, `openai.InternalServerError` for 500
and 503, and a plain `openai.APIStatusError` for 413, with the message in
the exception. It retries 500 and 503 automatically, honouring
`Retry-After`, before raising.

## Concurrency

Each service runs one inference at a time and queues the rest, up to
`queue_depth` (default 8) waiting or running; beyond that, requests fail at
once with `503`. The exception is STT streaming: up to `stt.streams` sessions
(default 2) run at once and share the engine (ADR 0020). A WebSocket stream holds its place for the whole session.
HTTP speech holds it only while synthesizing, not while the client
downloads. A speech client that hangs up before or during synthesis, or
while queued, over HTTP or Wyoming, frees its place at the engine's next
generation step, so its next request does not wait for the audio it abandoned; an engine that renders a
sentence in one call (Kokoro, Soprano) finishes that sentence first. Wyoming
requests share the same queue. How the queue works is
described in [architecture.md](architecture.md#inference-queue).

## Wyoming protocol

The stt service listens on TCP 10300 and the tts service on TCP 10200
(`wyoming_port` in each section). The listeners share the loaded model and
the queue with the HTTP routes. Home Assistant setup is in
[home-assistant.md](home-assistant.md); this section is for other Wyoming
clients. The design notes and what Home Assistant's integration expects are
in [ADR 0011](decisions/0011-wyoming.md).

**Speech-to-text.** `Describe` answers `Info` with one `AsrProgram` named
`voiced` and one model whose name is the model id and whose languages are
the [served languages](#languages). Send `Transcribe` (with an optional
`language`), `AudioStart`, `AudioChunk` events, then `AudioStop`; the answer
is `Transcript`. Sample widths of 1, 2 or 4 bytes, 1 to 16 channels and
rates from 8 to 192 kHz are accepted and converted to mono 16 kHz; anything
else, or a format field of the wrong type, is an `invalid_request_error`. `requires_external_vad` is true: the client
decides when speech ends.

Bounds on one utterance, so a client cannot hold the GPU indefinitely:

| Bound | Value |
|---|---|
| Audio per utterance | `stt.max_upload_mb` worth of 16-bit 16 kHz audio, about 26 minutes at the default 50 MB |
| Wall-clock time per utterance | that duration plus `server.session_idle_s` |
| Silence between chunks | `server.session_idle_s` (default 30 s); then the connection is dropped as a disconnect |
| One chunk | 1 MB; Home Assistant sends about 2 KB |
| Chunks queued ahead of the engine | 64; a client that outruns a busy GPU is slowed down through TCP, and gets `server_busy` if the wait exceeds `session_idle_s` |
| Audio format | Rate, width and channels must stay as announced in `AudioStart` |

A session that fails for any reason gets one `Error`. The connection stays
open while the client finishes writing the utterance: its remaining
`AudioChunk` events are read and dropped, and the connection closes at
`AudioStop` (or `server.session_idle_s` after the failure, if no `AudioStop`
comes). A client that writes the whole utterance before it reads, as Home
Assistant does, therefore reads the `Error` instead of a reset. Send the next
utterance on a new connection.

Bounds on every connection, on both listeners:

| Bound | Value |
|---|---|
| One event's `data` | 1 MB, which also limits a `Synthesize` text to roughly 170 000 characters |
| One event's payload | 1 MB |
| One event's header line | 64 KiB |
| Time to receive the rest of an event after its header | `server.session_idle_s` |
| Silence between events | 5 minutes; then the connection is closed |
| Open connections per listener | 64; more are closed at once |

An event over a size bound is refused from its header, before its body is
read: the client gets one `Error` (`invalid_request_error`, text starting
`BadEvent`) and the connection closes. A client that keeps sending the body
may see the connection reset before it reads that `Error`.

**Text-to-speech.** `Info` lists one `TtsProgram` named `voiced` with a
`TtsVoice` per served voice (name is the voice id, description is the voice
name and gender) and `supports_synthesize_streaming` true. While the model
is loading, `Describe` answers `Error` `server_busy`, because the voice list
needs the loaded engine.

- Plain `Synthesize` answers `AudioStart`, `AudioChunk` events and
  `AudioStop`, 24 kHz 16-bit mono.
- Streaming `SynthesizeStart`, `SynthesizeChunk`..., `SynthesizeStop`
  synthesizes sentence by sentence as text arrives and ends with `AudioStop`
  then `SynthesizeStopped`. A plain `Synthesize` received inside a stream is
  ignored, since Home Assistant sends the whole message again that way.
  Each sentence takes the GPU separately, so a conversation agent pausing
  between sentences does not block other requests.
- Text is capped at `tts.max_input_chars`: the whole message for
  `Synthesize`, the running total for a stream.
- The voice must be one `Describe` lists, as on HTTP; no voice means
  `tts.default_voice`.
- A stream that fails (its `SynthesizeStart` refused, its text over
  `tts.max_input_chars`, the engine failing mid-stream) gets one `Error`. Its
  remaining chunks and the compatibility `Synthesize` are read and dropped,
  and the connection closes at `SynthesizeStop` (or `server.session_idle_s`
  after the failure, if no `SynthesizeStop` comes), so a client that writes
  the whole stream before reading still reads the `Error`.
- Closing the connection stops the synthesis at the engine's next step and
  frees the GPU for the next request. A client keeps its side of the
  connection open until `AudioStop` (and `SynthesizeStopped` for a stream):
  shutting down its write side early is read as hanging up.

**Errors.** An `Error` event whose `text` starts with the exception name and
whose `code` is one of:

| Code | Cause | Connection |
|---|---|---|
| `server_busy` | Queue full, or model still loading | closed |
| `invalid_request_error` | Unknown voice, unsupported language, text too long, unconvertible or oversized audio, an event with a missing or mistyped field | kept open, except after a failed STT session or stream or an oversized event |
| `engine_error` | The engine failed; health turns `degraded` | closed |

A client that disconnects or stalls past the timeouts is dropped without an
`Error`. Every write is bounded by `server.write_timeout_s`.

**Discovery.** With `bind` set to a non-loopback address and `zeroconf =
true` (the default), the listeners are advertised over mDNS as `voiced-stt`
and `voiced-tts` with service type `_wyoming._tcp.local.`.
