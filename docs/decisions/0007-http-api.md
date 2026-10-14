# 0007: HTTP API layer

Date: 2026-10-14. Status: accepted.

## Context

`/v1/audio/transcriptions` and `/v1/audio/speech` must work unmodified with
the official OpenAI clients, LiteLLM and Open WebUI, while the engines only
speak fixed PCM formats (16 kHz float32 in, 24 kHz int16 out). Something has
to decode arbitrary uploads and encode compressed responses, requests must be
bounded so a public-facing instance cannot be knocked over by one upload, and
voice-agent frameworks need a live streaming transcription channel.

## Decisions

### ffmpeg is an external dependency

Uploads are decoded by spawning `ffmpeg` (`-f f32le -ac 1 -ar 16000`), and
`mp3` / `opus` speech responses are encoded by piping PCM through it. The
alternatives were Python bindings (`av`, `pydub`, `soundfile`): they either
bundle their own ffmpeg builds (large wheels, licensing surface) or cover far
fewer formats. ffmpeg is one `brew install` away on every Mac, `voiced
doctor` checks for it, and the API fails predictably without it: uploads get a clear 500
`ffmpeg_missing` and `mp3`/`opus` responses are refused with the same error.
There is deliberately no second decoder in Python for the "ffmpeg missing"
case. Uploads are written to a temporary file before decoding because
containers such as `.m4a` keep their index at the end and cannot be decoded
from a pipe.

### Every speech response streams

`pcm` and `wav` are written chunk by chunk as the engine yields sentences.
The WAV header carries `0xFFFFFFFF` in both size fields ("length unknown"),
which browsers, ffmpeg and the common players treat as "read until EOF" and
which is also what the OpenAI endpoint does. `mp3` and `opus` (Ogg container)
stream through an ffmpeg pipe with a writer task feeding it, so the first
encoded bytes leave before the last sentence is synthesized. `audio/pcm` is
raw int16 little-endian mono at 24 kHz, matching OpenAI.

The body is an async generator wrapping `inference()` (ADR 0005); the route
primes it with one `anext()` *before* the response starts. That keeps the
status codes honest: a full queue is still a 503 and an unknown voice is
still a 400 instead of a truncated stream. The response class
(`ClosingStreamingResponse`) always calls `aclose()` on the body generator,
so a client that disconnects mid-stream frees the slot and any ffmpeg pipe
immediately rather than when the garbage collector gets to it.

### Limits

`stt.max_upload_mb` (default 50) bounds the transcription body. The
`Content-Length` header is checked first, and the ASGI `receive` channel is
wrapped so a chunked body that exceeds the limit is cut off while it is
still being read, before multipart parsing and before ffmpeg. Both paths
return 413 `payload_too_large`. `tts.max_input_chars` (default 4096, the
OpenAI limit) bounds `input`, also with 413.

### The `model` field

OpenAI clients always send a model (`whisper-1`, `tts-1`). Each voiced process
serves exactly one model, so the field is ignored, with one exception: naming
the model (or engine name) of *another installed engine* is a 400
`model_not_served`, because the caller clearly expected a different model
than the one running. Silently transcribing with the wrong model would be
worse than an error. The map of foreign names is built once at startup
(`voiced.api.foreign_models`), not by scanning entry points per request.

### Streaming transcription over WebSocket

`WS /v1/audio/transcriptions/stream` takes an optional JSON `config` frame,
then binary frames of int16 little-endian mono PCM (16 kHz unless `config`
says otherwise; other rates are resampled server-side), then `{"type":
"end"}`. The sample rate is per session on purpose: browsers and phones
capture at 44.1 or 48 kHz and should not have to resample client-side.
Frames may be any size; an odd trailing byte is carried into the next frame
rather than dropped, which would misalign every later sample. It sends `partial` events when the hypothesis changes, at most every
250 ms, then one `final` event and a normal close. One session holds one
inference slot for its whole life, so `queue_depth` also bounds concurrent
streams; overflow closes with 1013 (try again later). A session that receives
nothing for 30 s is closed with 1008 `idle` so a silent client cannot hold a
slot forever. Bad frames close with 1007, request-level engine errors with
1008, engine failures with 1011. A
bad API key is refused at the handshake (HTTP 403). The API key dependency
uses Starlette's `HTTPConnection` so the same check covers HTTP and
WebSocket routes.

### Status page

`GET /` is one HTML file with vanilla JavaScript and a few Lucide icons
inlined as SVG symbols: no build step, no CDN, nothing fetched at runtime.
It polls `GET /metrics.json`, which merges the `/health` snapshot with the
`voiced.metrics` snapshot (per-operation outcome counts and p50/p95 latency
over the last 1000 inferences, the last 50 errors, resident memory) and
engine metadata. Both routes are unauthenticated like
`/health`: the page would be useless behind a key, and the data it exposes is
operational (error messages, latencies), not user content. The test boxes on
the page call the `/v1` routes and send the API key the operator types in.

## Consequences

- `ffmpeg` joins `espeak-ng` as a documented external dependency.
- `python-multipart`, `websockets` (uvicorn's WebSocket backend) and `psutil`
  are core dependencies.
- Error bodies on `/v1` routes share one shape, `{"error": {"message",
  "type"}}`, which the OpenAI clients surface as `BadRequestError`,
  `AuthenticationError` and so on (handlers in `voiced.api.app`, ADR 0005).
- Metrics live in process memory and reset on restart; the watchdog's
  restarts therefore also clear the error log. Persisting them is out of
  scope for v1.
