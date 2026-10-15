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
fewer formats. ffmpeg is one `brew install` away on every Mac, and
`voiced doctor` reports a missing ffmpeg as a failure. Without it the API
fails predictably: uploads and `mp3`/`opus` requests get a 500
`ffmpeg_missing` (`voiced.api.ffmpeg.FFmpegMissing`, an `APIError`, so one
handler renders it for every route). There is deliberately no second decoder
in Python for the "ffmpeg missing" case. Uploads are written to a temporary
file before decoding because containers such as `.m4a` keep their index at
the end and cannot be decoded from a pipe. The file is anonymous (unlinked
from the start) and ffmpeg reads it through an inherited descriptor
(`-fd N -i fd:`) with `-protocol_whitelist fd`: ffmpeg picks the demuxer
from the content, and formats that name other inputs (an `ffconcat` list, a
playlist) would otherwise open files on the server's behalf, which is how a
named upload in `/tmp` could have another audio file there transcribed (ADR
0016). This needs an ffmpeg with the `fd` protocol, which `voiced doctor`
checks.

### Every speech response streams

`pcm` and `wav` are written chunk by chunk as the engine yields sentences.
The WAV header carries `0xFFFFFFFF` in both size fields ("length unknown"),
which browsers, ffmpeg and the common players treat as "read until EOF" and
which is also what the OpenAI endpoint does. `mp3` and `opus` (Ogg container)
stream through an ffmpeg pipe with a writer task feeding it, so the first
encoded bytes leave before the last sentence is synthesized. `audio/pcm` is
raw int16 little-endian mono at 24 kHz, matching OpenAI.

`voice` must be one of the voices `/v1/audio/voices` lists, which are the
engine's voices in the configured languages (ADR 0005). The route checks
this before taking the slot, so a voice the engine knows but whose language
is not configured is a 400 `UnknownVoice`, and so is a `tts.default_voice`
outside the configured languages.

The engine runs inside `inference()` in its own task, which hands chunks to
the response body through a queue (ADR 0005). The route primes the body with
one `anext()` *before* the response starts. That keeps the status codes
honest: a full queue is still a 503 and an engine error is still a 400
instead of a truncated stream.

Keeping `inference()` out of the body is what lets a hang-up be counted. A
server stops a response by cancelling the task that streams it; with the
block inside the body, that `CancelledError` landed inside `inference()`,
which records nothing for cancellation, so hang-ups never showed up in the
metrics. Now the body only sets a flag when it stops, and the synthesis task
raises a disconnect at the next chunk, which `inference()` counts. The slot
is therefore released after the chunk in flight, which a blocking model call
required anyway (`run_blocking` waits for its thread). Only a hang-up while
the engine is still producing is a disconnect; one after synthesis finished
records `ok`, since the audio was produced and only its delivery failed.

Because the slot is free once synthesis ends, a client that stops reading no
longer holds the GPU, but it would still hold its response task, its queued
audio and, for mp3 and opus, an ffmpeg process. Every `send` of the response
is therefore bounded by `server.write_timeout_s` (default 10 s); after that
the client counts as gone and all of it is released. Releasing ffmpeg needs
one more step: since Python 3.12 `Process.wait()` returns only once every
pipe is closed, and ffmpeg's stdout, full because nobody reads it, is paused
and never sees EOF, so the encoder reads stdout to its end before waiting.

The response cannot complete for a client that is not reading, so uvicorn
logs one line per such client ("ASGI callable returned without completing
response") and closes the connection. That line is kept: a client that
stalls for 10 s is worth a line. uvicorn's close still waits to flush what is
buffered, so the socket lingers until the client reads or leaves, and a stop
would wait for it indefinitely. `voiced serve` therefore gives uvicorn a 10 s
graceful-shutdown timeout, below launchd's ExitTimeOut, so stalled clients
cannot turn a stop into a SIGKILL.

Operational note: uvicorn logs that line at ERROR level, as
`ASGI callable returned without completing response`, once per speech
client cut off at `write_timeout_s`. It records a client that stopped
reading, which is a disconnect, not a server error: nothing failed on the
server side and health is untouched. (A client that closes the connection
itself does not produce the line; uvicorn only logs it for a connection
that is still open.) Do not alert on it. Alert on
`/health` turning `degraded` or on the `failure` count on the status page
instead.

The queue is unbounded: synthesis runs at GPU speed and frees the slot as
soon as it finishes, however slowly the client reads. `tts.max_input_chars`
bounds what can pile up, though less tightly than it looks: normalization
expands digits and symbols into words, so 4096 characters of the one-half
sign (U+00BD) became 35 minutes of audio (100 MB) in a measurement, against
6 minutes (17 MB) for 4096 characters of the README's prose (ADR 0016).
`inference()` therefore times the synthesis, not the client's download.

The response class (`ClosingStreamingResponse`) always calls `aclose()` on
the body generator, so a client that disconnects frees any ffmpeg pipe
immediately rather than when the garbage collector gets to it. A hang-up is
not an error: the response class absorbs it, so neither the app nor the
server logs a traceback for it.

### Limits

`stt.max_upload_mb` (default 50) bounds the transcription upload in two
steps. The ASGI `receive` channel is wrapped so the request body is cut off
while it is being read, before multipart parsing finishes and before ffmpeg,
whether or not the client sent `Content-Length`; the cap allows 64 KB of
multipart framing on top of the limit. The parsed file is then held to the
exact limit. Both return 413 `payload_too_large`, and so does an upload
that decodes to more than `max_upload_mb` of 16-bit 16 kHz audio (the bound
a Wyoming utterance has): a small file can decode to hours of audio, so
ffmpeg's output is counted while it is read and ffmpeg is stopped past the
bound. Decoding runs before the request queues for the slot, so the queue
does not bound it; `ServiceContext.uploads` admits at most `queue_depth`
decodes at once and answers 503 `server_busy` beyond that. A malformed multipart body
is Starlette's own 400, rendered in the shared envelope. A client that hangs
up mid-upload is not an error and is not logged as one. An upload ffmpeg
cannot decode is a 400 `invalid_audio` with a fixed message; ffmpeg's own
output goes to the debug log, since it names temporary files.
`tts.max_input_chars` (default 4096, the OpenAI limit) bounds `input`, also
with 413.

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
Frames may be any size. They go through `voiced.audio.PCMConverter`, which
carries a partial sample and the resampling position into the next frame;
dropping an odd trailing byte would misalign every later sample.

The server sends `partial` events when the hypothesis changes, at most every
250 ms, then one `final` event and a normal close (1000). The whole session
runs inside one `inference()` block, so `queue_depth` also bounds concurrent
streams; like a Wyoming utterance it carries at most `max_upload_mb` of
16-bit 16 kHz audio and lasts at most that long plus `session_idle_s`. The
close code follows the outcome table in ADR 0005: busy 1013, client error
1008 (a bad frame, an unsupported language or sample rate, a stream over the
bound), failure 1011, disconnect 1001. Every socket read is bounded by
`server.session_idle_s` (default 30) and every write by
`server.write_timeout_s`, so a silent or half-open client cannot hold a slot.

An idle session therefore closes with 1001, as a disconnect that leaves
health alone. The first version of this API closed it with 1008 `idle`; that
changed when the WebSocket route adopted ADR 0005's rule that a transport
does not convert timeouts itself. Likewise a malformed frame closes with
1008 (a client error) rather than the 1007 of that first version.

Closing a socket whose peer already dropped the TCP connection raises in the
server; the route swallows that, since the outcome is already recorded and a
traceback would be noise. A bad API key is refused at the handshake (HTTP
403). The API key dependency uses Starlette's `HTTPConnection`, so the same
check covers HTTP and WebSocket routes.

### Status page

`GET /` is one HTML file with vanilla JavaScript and a few Lucide icons
inlined as SVG symbols: no build step, no CDN, nothing fetched at runtime.
It polls `GET /metrics.json`, which merges the `/health` snapshot with the
`voiced.metrics` snapshot (per-operation outcome counts and p50/p95 latency
over the last 1000 inferences, the last 50 errors, resident memory) and
engine metadata. Both routes are unauthenticated like
`/health`: the page would be useless behind a key, and the data it exposes is
operational (latencies, outcome counts, error times), not user content. An
error's detail can echo what a client sent, such as an unknown language tag,
so with `server.api_key` set the feed omits each error's `detail` and keeps
its time, operation and outcome, and `/health`'s `last_error` (also in the
feed) keeps only the exception name. `inference()` logs every failure, so
the message is still in the service log. The test boxes on
the page call the `/v1` routes and send the API key the operator types in.

## Consequences

- `ffmpeg` joins `espeak-ng` as a documented external dependency.
- `python-multipart`, `websockets` (uvicorn's WebSocket backend) and `psutil`
  are core dependencies.
- Error bodies share one shape, `{"error": {"message", "type"}}`, which the
  OpenAI clients surface as `BadRequestError`, `AuthenticationError` and so
  on (handlers in `voiced.api.app`, ADR 0005). That includes Starlette's own
  errors such as a malformed multipart body, 404 and 405.
- Metrics live in process memory and reset on restart; the watchdog's
  restarts therefore also clear the error log. Persisting them is out of
  scope for v1.
