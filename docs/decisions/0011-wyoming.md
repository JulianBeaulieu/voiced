# 0011: Wyoming listeners

Date: 2026-10-14. Status: accepted.

## Context

Home Assistant talks to local speech services over the Wyoming protocol
(`wyoming` package, TCP, newline-delimited JSON events with binary payloads).
voiced needs an STT listener (port 10300) and a TTS listener (port 10200)
that Home Assistant can add as Wyoming services. Each must share the loaded
model, inference queue and health state with the HTTP API of the same process
(one model in memory, one GPU queue), support Home Assistant's streaming
synthesis so speech starts before the conversation agent finishes, and be
discoverable over zeroconf.

## Decisions

### In-process, inside the FastAPI lifespan

`voiced.wyoming.server.attach(app, ctx, host, port, handler_cls, service_name)`
wraps the app's lifespan: it starts a plain `asyncio.start_server` listener on
the same event loop and stops it on shutdown. Since ADR 0005 the engine loads
in a background task so the socket binds first, so the listener may answer
before the model is warm; the handlers call `ctx.check_loaded()` inside
`inference()`, which `classify` maps to `busy` until `load()` finishes.
`build_stt_app` / `build_tts_app` call `attach` and pass the handler class and
service name, so the listener never branches on `ctx.service`; `voiced serve`
and the tests get the listener for free and `cli.py` needed no change. The
listener holds the same `ServiceContext` as the routes: one engine, one
`InferenceQueue`, one `HealthState`, one `Metrics`.

The `wyoming` package's `AsyncTcpServer` was not used because it installs its
own SIGTERM handler (which would fight uvicorn's) and hides the bound socket,
which tests need for ephemeral ports. Its `AsyncEventHandler` base class is
reused for the per-connection event loop.

### Everything goes through the shared core (ADR 0005)

The handlers do not implement their own slot/health/metrics policy. Every
engine call runs inside `async with inference(ctx, Operation.X)`
(`voiced.inference`), which takes the slot, times the block, and records the
outcome in health and metrics, so the status page now sees Home Assistant
traffic the same way it sees HTTP traffic. Language matching and voice
filtering come from `ServiceContext` (`voiced.context`): `check_language`,
`voices`, `languages` and `check_loaded`; the handlers never match tags
themselves. PCM conversion is `voiced.audio.PCMConverter`, and text
normalization and sentence splitting are `voiced.text.normalize` and
`voiced.text.split_sentences`.

### STT: one slot per utterance, run as a session task

An utterance spans several events while the slot is held, so the session runs
as its own task (ADR 0005: an `AsyncExitStack` closed with `aclose()` would
record `ok` on a failure). The task owns `async with inference(ctx, STREAM)`:
it builds a `PCMConverter` from the `AudioStart` format, consumes `AudioChunk`s
from an `asyncio.Queue` with `bounded(queue.get(), session_idle_s)` so a
half-open satellite's slot is released, finalizes on the `None` sentinel the
connection handler enqueues at `AudioStop`, and writes the `Transcript`. A
later chunk whose rate, width or channels differ from the `AudioStart` format
is an `EngineError`.

Limits. An utterance may carry at most `stt.max_upload_mb` of 16-bit audio at
16 kHz: `max_utterance_s` is about 26 minutes at the default 50 MB. More audio
is an `EngineError`, and so is a session that outlives that duration plus
`session_idle_s` (a wall-clock `asyncio.timeout` inside `inference()`), so a
client trickling tiny chunks cannot hold the slot forever. The queue between
handler and task holds 64 chunks (a few seconds of Home Assistant audio), and
one chunk may carry at most `MAX_CHUNK_BYTES` (1 MB; Home Assistant sends about
2 KB), so queued audio stays under 64 MB per connection whatever chunk size a
client picks; a bigger chunk is one `Error` and a close, decided from the
event's header before its payload is read (see the amendment below). The handler awaits
`put` under `bounded(..., session_idle_s)`, so a client that outruns a busy GPU
is pushed back through TCP. The queue only stays full while the session cannot
drain it, which means the server is the slow side (the session is still
waiting for the slot, or the engine is slow), so running out of time there is
`QueueFull` and the client gets `server_busy` rather than a dropped connection.
That error is raised outside `inference()`, so, per ADR 0005, it is not counted
in metrics; the request was admitted to the queue and gave up waiting, like an
HTTP client that disconnects while queued.

The connection handler only feeds the queue. The task reports its own outcome
(the one place) and on any failure closes the connection, whatever the
outcome: the handler never feeds a session that has ended, and a failed
session never restarts on the same connection, so a client that keeps
streaming after an error cannot get a transcript of the leftover audio or a
fresh size budget.

### TTS: streaming with per-sentence slots

Plain `Synthesize` runs the engine inside one `inference(ctx, SPEECH)` block
and emits `AudioStart`, `AudioChunk`s, `AudioStop` at 24 kHz 16-bit mono.

The streaming form (`SynthesizeStart`, `SynthesizeChunk`..., `SynthesizeStop`)
accumulates text in `SentenceAccumulator`, which runs `voiced.text.split_sentences`
over the buffer and releases every sentence but the last, which may still be
growing ("Call Dr." before "Smith"); a newline releases it too. A synthesis
task synthesizes each sentence while the handler keeps reading text, so the
first audio goes out while the conversation agent is still generating.

Each sentence takes its own `inference(ctx, SPEECH)` slot instead of holding
one for the whole stream. A stream can idle for seconds between sentences while
an LLM thinks; holding the GPU across that gap would block every HTTP request
for the duration. The wait for the next sentence is not bounded because no slot
is held then; only the slot-holding `AudioChunk` writes are. The cost is that
`QueueFull` can in theory hit mid-stream (after audio has already started),
which is reported as an `Error` like everywhere else. `max_input_chars` caps
the request text (the whole message for `Synthesize`, the running total for a
`SynthesizeChunk` stream); a stream that goes over gets one `Error` and is
closed.

`AudioStart` is written exactly once per request through `_ensure_audio_start`
and `_end_audio`, so the three code paths (plain, streaming, empty text) cannot
disagree about whether the header has been sent.

### Error policy and no authentication

The handlers never catch `QueueFull`/`EngineError`/`Exception` to decide what
happened. `inference()` records the outcome; one `except Exception` then maps
`classify(exc)` through `_WIRE`, the Wyoming counterpart of the HTTP `_HTTP`
table: `busy` sends `Error(server_busy)` and closes; `client_error` (an
unknown voice, unsupported language, over-long input, unconvertible audio)
sends `Error(invalid_request_error)` and keeps the connection for
request/response events; `disconnect`
(`DISCONNECT_ERRORS`, including a `ClientTimeout` from a bounded read or write)
is cleaned up quietly with no `Error`; `failure` sends `Error(engine_error)`.
The error text starts with the exception type. For the streaming paths the
session or synthesis task is that one place, reporting immediately rather than
waiting for the next client event (which a client blocked on a read never
sends), and a broken stream always closes the connection. Requests that arrive
before the model has loaded get `server_busy` (`check_loaded()`); that includes
the TTS `Describe`, whose voice list needs the loaded engine, so Home Assistant
shows the entry as not ready until warm-up ends and then retries. Writes are bounded with
`bounded(write, write_timeout_s)`, and a write to a peer that has already gone
is guarded by `writer.is_closing()` so it raises `ConnectionResetError`
(a disconnect) rather than a `TypeError` on a closed transport.

The Wyoming protocol has no authentication, so both ports are unauthenticated;
the HTTP `api_key` does not apply to them. Bind to a trusted network only (see
`config.example.toml`).

### Zeroconf only when reachable

`voiced-stt` and `voiced-tts` are registered as `_wyoming._tcp.local.` with
the bound port, with the `[server] zeroconf` key (default `true`) as the
switch. Nothing is advertised when `bind` is a loopback address: no other
host can connect to it, and the advertised address would be 127.0.0.1, which
Home Assistant would resolve to itself. For `0.0.0.0` the advertised address
is the interface the kernel routes LAN traffic through (UDP connect trick, no
packet is sent). Registration failures are logged and never stop the service.

## What Home Assistant actually expects

Read from `homeassistant/components/wyoming` (core `dev`, 2026-10):

- Only `asr[0]` and the first *installed* `TtsProgram` are used. Entity names
  and config-entry titles come from the program `name`, so both services show
  up as "voiced"; the ports tell them apart. STT languages are the union of
  the installed models' `languages`. HA does not read `Transcript.language`.
- `AsrProgram.requires_external_vad` (default true) tells HA to run its own
  end-of-speech detection and send `AudioStop`; the listener never has to
  decide when the user stopped talking.
- Streaming TTS: HA sends `SynthesizeStart`, the chunks, **then a plain
  `Synthesize` with the full message** (for servers without streaming support)
  and finally `SynthesizeStop`. Inside a stream the `Synthesize` must be
  ignored or the reply would be spoken twice. HA's streaming reader finishes
  on `SynthesizeStopped`, not on `AudioStop`, so both are sent, in that order.
  The non-streaming reader finishes on `AudioStop`.
- The first `AudioStart` becomes the WAV header HA forwards; later
  `AudioStart`s are ignored. voiced sends exactly one per request.
- Voices are grouped by their `languages` entries and shown as
  `description or name`. Matching against the pipeline language uses HA's
  language utilities, so `en-US` voices satisfy an `en` pipeline.
- Discovery: the config-flow unique id is `<zeroconf instance name>_<program
  name>`, so the two listeners (`voiced-stt`, `voiced-tts`) register as two
  separate entries. HA polls `Describe` on a fresh connection periodically
  (`WyomingInfoCoordinator`), so `Describe` must stay cheap and must work at
  any time.
- On any error HA logs and returns an error result; it never retries a
  request. The `Error.code` is informational only.

## Consequences

- `zeroconf` is a core dependency (pure Python plus `ifaddr`).
- Two config-entry titles both read "voiced" in Home Assistant. Renaming the
  programs (`WYOMING_ASR_NAME` / `WYOMING_TTS_NAME`) would change the
  entity names HA users see, so it should happen before the first release if
  at all.
- Sentence-level streaming means the Wyoming path never sends text longer
  than a sentence to the engine; engines that do their own chunking see no
  difference for plain `Synthesize`.

## Amendment (ADR 0016)

The `wyoming` package's reader reads whatever `data_length` and
`payload_length` a header announces before anything can look at the event,
and waits for the next event forever, so the `MAX_CHUNK_BYTES` check above
ran after a client had already made the process buffer its payload, of any
size. `VoicedEventHandler.run` now reads events itself: sizes are checked
from the header (1 MB payload; 1 MiB of `data`, more if `max_input_chars`
needs it), the rest of an event must arrive within `session_idle_s`, a
connection silent between events for five minutes is closed, and each
listener holds at most 64 connections, refusing more at once, so idle
connections cannot use up the file descriptors the HTTP API shares. A
malformed event (a missing field, a sample rate given as a string) is an
`invalid_request_error`; before, it raised inside the slot and marked the
service degraded. `PCMConverter` accepts 8 to 192 kHz and 1 to 16 channels
(a rate of 1 Hz made each input sample 16000 output samples). Voices are
checked against `ServiceContext.voices()` before the engine sees them, as on
HTTP.
