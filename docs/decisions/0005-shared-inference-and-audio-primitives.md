# 0005: Shared inference and audio primitives

Date: 2026-10-14. Status: accepted.

## Context

The first versions of the HTTP API and the Wyoming listeners each
re-implemented the same policy around every engine call: take a queue slot,
time the inference, record success or failure into health, record metrics,
and turn `QueueFull`, `EngineError` and everything else into a wire error.
They also had two PCM-to-float32 converters and worked around import cycles
in `voiced.api` with function-local imports. Every new transport or route
would have copied that a third time, with a third chance of getting the
health rules wrong. The modules are listed in ADR 0001; this records the
rules they enforce.

## Decision

### One way in: `inference()`

Every engine call runs inside `async with inference(ctx, Operation.X):`
(`voiced.inference`). It takes the slot (`InferenceQueue.slot()`, the
queue's only interface), times the block, records the outcome, and
re-raises. It is the only writer of metrics and of `HealthState`'s
inference fields. `ServiceContext` lives in `voiced.context` because both
transports use it; `Operation` (`transcribe`, `stream`, `speech`) is a
shared enum so HTTP and Wyoming report the same rows.

### Outcome taxonomy

| Outcome | Raised | Health | Metrics | HTTP | Wyoming |
|---|---|---|---|---|---|
| `ok` | nothing | `record_success(ms)` | latency sample | 200 | result event |
| `busy` | `QueueFull`, `EngineNotLoaded` | untouched | count + error log | 503 + `Retry-After` | `Error`, disconnect |
| `client_error` | other `EngineError` | untouched | count + error log | 400 | `Error`, keep connection |
| `disconnect` | `DISCONNECT_ERRORS` | untouched | count only, debug log | close if still open (WebSocket 1001) | close if still open (handler returns `False`) |
| `failure` | any other `Exception` | `record_error` (degraded) | count + error log | 500 | `Error`, disconnect |

`client_error` leaves health alone because `voiced.engines.base` defines an
`EngineError` as the client's fault; a stream of bad requests must not make
the watchdog restart a healthy process.

`DISCONNECT_ERRORS` is every way a peer can hang up: `ConnectionError`
(resets, aborts, broken pipes, and `ClientTimeout`), `IncompleteReadError`,
Starlette's `WebSocketDisconnect` and `ClientDisconnect`, and uvicorn's
`ClientDisconnected`. It is deliberately not every `OSError`: an engine that
cannot open its weights raises `FileNotFoundError` and that is a failure.
The cost is that a `BrokenPipeError` from an engine's own subprocess also
counts as a disconnect; engines should wrap those in their own error.

`ServiceContext.check_loaded()` raises `EngineNotLoaded` until `load()` has
finished. Classifying it as `busy` tells clients to retry, on every
transport, without a separate "starting" code path.

`ServiceContext.languages` is the engine's languages intersected with the
configured ones, matched on the primary subtag in either direction (`_` is
read as `-`), reporting the narrower tag of each pair. `voices()` filters by
the same predicate, and `check_language(hint)` resolves a client's hint to
the engine's own code (preferring an exact tag, then the advertised tag for
that subtag, then any tag with that subtag) or raises `UnsupportedLanguage`.
Both transports use these three and never match language tags themselves.
Because matching is by subtag, a config of `["en-gb"]` against an `["en-US"]`
engine advertises `en-gb` while the voices it serves are the engine's en-US
ones; that is intended. The `languages` settings may not be empty, and a
service whose config and engine share no subtag (so `languages` is empty)
logs a warning after load rather than failing, since the engine is otherwise
healthy.

`voiced.audio.UnsupportedAudioFormat` subclasses `EngineError`, so a bad
sample width or rate is a client error on every transport.

### Rules for transports

- **Inside `inference()`, raise only `EngineError` subclasses for client
  mistakes.** Anything else, including a transport's own request errors
  (`APIError`, a bad WebSocket frame), is classified as a server failure.
- **A transport never catches `QueueFull`, `EngineError` or `Exception` to
  decide what happened.** It has one `except Exception as exc:` after the
  `inference()` block that reads `classify(exc)` and looks the outcome up in
  its own table (`_HTTP` in `voiced.api.app`; Wyoming keeps a table of the
  same shape). That `except` records nothing: `inference()` already did.
- **Bound every socket await made while holding a slot** with
  `bounded(aw, timeout_s)`: reads with `server.session_idle_s` (default 30),
  writes with `server.write_timeout_s` (default 10). It raises
  `ClientTimeout`, a `ConnectionError`, so an idle or half-open peer is a
  `disconnect` and never pins the GPU. Transports do not convert timeouts
  themselves.
- **Sessions that span several events** (Wyoming STT: `AudioStart`, chunks,
  `AudioStop`) run as one task that owns `async with inference(...)` and
  consumes events from an `asyncio.Queue`; the connection handler only feeds
  the queue and cancels the task on teardown. Holding the slot through an
  `AsyncExitStack` closed with `aclose()` does not work: `aclose()` passes no
  exception, so a failure followed by cleanup records `ok` and clears
  `last_error`, and an idle teardown records a 30-second success.
- **Streaming HTTP responses** must surface `QueueFull` and pre-audio
  `EngineError` before the response starts, and must count a client that
  hangs up while the engine is still producing. (A hang-up after synthesis
  finished records `ok`: the audio was produced, only its delivery failed.)
  Run the engine inside `async with inference(...)` in its own task that
  hands chunks to the body through a queue; the body is an async generator
  the route primes with one `anext()`. When the body stops early it sets a
  flag, and the task raises a disconnect at the engine's next step, which
  `inference()` counts (see the amendment of 2026-10-21; it was the next
  chunk before). Do not put `inference()` in the body
  generator itself: a server stops a response by cancelling its task, and
  that `CancelledError` lands inside the block, which records nothing (see
  Cancellation). The protocol between the two: the task forwards its
  exception through the queue (so priming still raises `QueueFull` or a
  pre-audio `EngineError` before the headers) and always queues an end
  marker last, in a `finally`; the route keeps a strong reference to the
  task, since asyncio keeps only weak ones; and the body's `finally` only
  sets the flag and never awaits the task, because anyio re-cancels every
  await in a cancelled scope. Every `send` of the response is bounded by
  `server.write_timeout_s`, so a client that stops reading counts as gone.
  The multi-event session task (previous bullet) has three more
  obligations. It reports its own outcome:
  on any exception it maps `classify` through the transport's table, closes
  the connection and ends, instead of leaving the error for the connection
  handler to re-raise on the next event (a client waiting for the result
  sends no next event, so the error would wait for teardown). The handler
  never feeds a session that has ended: once the task is done it closes
  rather than queueing into it or starting a new session on the same
  connection, so a client that keeps streaming after an error cannot get a
  result for the leftover audio or a fresh size budget. And it compares each
  chunk's rate, width and channels with the `PCMConverter` it built from
  `AudioStart`, raising an `EngineError` if they change. The queue between
  the handler and the task is bounded in chunks and each chunk in bytes (the
  handler awaits `put` under `bounded`, so a client that outruns a busy GPU is
  pushed back and, once the wait runs out, told `QueueFull`), and the whole
  session has a wall-clock bound so a client trickling tiny chunks cannot
  hold the slot forever (ADR 0011).
- **Never nest `inference()` in one task.** The second call would wait for
  the slot its own task holds; `slot()` raises `RuntimeError` instead. A
  child task spawned inside a slot is not caught by that check and would
  deadlock the same way: never take a slot from one.
- Transports do not convert PCM themselves; they use `voiced.audio`.

### Blocking calls and cancellation

`asyncio.to_thread` gives control back the moment the caller is cancelled,
while the thread keeps running. Inside a slot that frees the slot during GPU
work, and the next request starts a second MLX call concurrently.
`voiced.threads.run_blocking` waits for the thread before re-raising
`CancelledError`. Engines must use it for every blocking model call,
including in `load()` (see below).

`CancelledError`, and `GeneratorExit` when a streaming generator is closed
early, pass through `inference()`: nothing is recorded and the slot is
released once any `run_blocking` thread has finished.

### Timing

`inference()` times the block while the slot is held, excluding the wait for
the slot. For streaming (an STT session, a Wyoming TTS sentence loop) the
block is the whole stream, so `last_inference_ms` and the p50/p95 are
full-session numbers. For HTTP speech the block is the synthesis only, since
the response is delivered from the queue after the slot is released.
Time to first audio is a benchmark concern and is measured by the benchmark,
not by the server.

### HTTP specifics

- `voiced.api.app` has one handler for `QueueFull` and `EngineError` that
  reads `_HTTP[classify(exc)]`, plus handlers for `APIError` (its own
  status), `RequestValidationError` (400, as OpenAI does, so the `openai`
  client raises `BadRequestError` instead of a generic 422 error) and
  `Exception` (500, logged; it does not touch health, because
  `inference()` already recorded any engine failure and a bug outside a
  slot is not one). All use `{"error": {"message", "type"}}`.
- Starlette runs the class-specific handlers for WebSocket routes too, both
  during the handshake and after `accept()`; they close the socket with
  `voiced.api.context.WS_CLOSE`, the same table a WebSocket route uses for
  in-session errors. Only the catch-all `Exception` handler is HTTP-only, so
  a WebSocket route still ends with its own `except Exception` +
  `classify`. A close before `accept()` reaches the client as HTTP 403, not
  as close code 1008; uvicorn needs the `websockets` package for this.
- `require_api_key` takes an `HTTPConnection`, so the same dependency guards
  HTTP routes and WebSocket handshakes, and compares with
  `hmac.compare_digest`.
- Route modules import from `voiced.api.context` and `voiced.context`, never
  from `voiced.api.app`, so `app.py` imports routes at module level.
- Checks that raise `APIError` (such as rejecting a model name that belongs
  to another installed engine) stay in `voiced.api`; they are HTTP request
  validation, not inference policy.

### Startup: bind first, load in the background

The lifespan starts `engine.load()` as a background task and yields at once,
so uvicorn binds immediately. Until the load finishes, `/health` answers 503
with `status: "starting"` (schema unchanged) and `/v1` answers 503 through
`check_loaded()` (type `server_busy`, message `EngineNotLoaded: model is still
loading`). This only works if `load()` does its blocking work through
`run_blocking`; a `load()` that blocks the event loop stalls `/health` as
well. If `load()` raises, the process logs the traceback and exits with
status 1, so launchd restarts it. If the process is asked to stop (SIGTERM)
while loading, it exits at once rather than waiting out a load thread that
cannot be interrupted, since launchd kills a job that ignores SIGTERM for
about 20 seconds (`ExitTimeOut`).

Before this, `/health` refused connections during a long model load, which
the watchdog could not tell apart from a crash.

`HealthState.snapshot(queue_depth)` takes the depth from the queue, so
`GET /health` reads state and never writes it.

## Consequences

- A new transport (or route) is a mapping table from `Outcome` to its wire
  format plus its protocol parsing; it cannot get health or metrics wrong.
- Metrics are keyed by `Operation`, not route or status code, so the status
  page shows STT and TTS the same way whichever transport served them.
- Errors raised outside any `inference()` block (request validation, auth, a
  bug in a route) reach neither metrics nor health; Wyoming follows the same
  rule.

## Amendment (2026-10-21)

A hang-up ends the synthesis at the engine's next step.

The flag between an HTTP speech body and its synthesis task was checked once
per chunk. A sentence engine drops its empty pieces, so for an engine that
renders its audio every few hundred generation steps (about half a second
apart) the abandoned synthesis kept the slot until its next render, and the
request a client sent after hanging up (a barge-in, or a benchmark reading
only the first chunk) waited behind it. Wyoming had the same delay: a plain
`Synthesize`, and the end of a stream after `SynthesizeStop`, run inside
`handle`, so nothing read the connection until a write to it failed.

The flag is now a `voiced.inference.HangUp`, given to `inference()`, which
enters it around the slot, so it ends the wait for the slot too:

```python
hang_up = HangUp()
async with inference(ctx, Operation.SPEECH, hang_up):
    ...
```

`hang_up.set()`, called by whoever sees the client go, cancels the block's
current await and raises `ClientHungUp`, a `ConnectionError` and so a
`disconnect`. It is built on `asyncio.timeout`, which already turns its own
cancel into an exception and tells it apart from any other cancel of the
task: a server's or teardown's cancel still passes through as
`CancelledError` and records nothing, as above. A `HangUp` set before its
block raises `ClientHungUp` on entry, without waiting for the slot or
starting an engine call (an expired `asyncio.timeout` would cancel only at
the first await that suspends, by which time the first call is already on
the model thread). `set()` after the block does nothing, so a hang-up after
synthesis finished still records `ok`.

The seam is the await every engine already has: `run_blocking` and
`ModelThread` wait for the current thread call to finish before the cancel
propagates, so the GPU is idle when the slot is released, and
`SentenceTTSEngine`'s `finally` closes the sentence's piece generator (a
failure to close is logged, so it cannot turn the disconnect into a
failure). Each `next()` of `_synth_chunks` is one such call, so an engine
that yields between generation steps (empty pieces included) stops at the
next step; one that renders a sentence in one call (`_synth_sentence`:
Kokoro, Soprano) stops when that sentence is done. No engine or
`TTSEngine.synthesize` contract changed. Since ADR 0030, an engine whose one
step is a whole sentence can stop sooner: `SentenceTTSEngine` calls its
`_interrupt()` hook when the request is cancelled during a step
(`ModelThread.run_stoppable`), and OmniVoice stops at its next forward pass. A stop flag passed to the sentence
loop was the alternative; it would have meant a new `synthesize()` parameter
for every engine, for the same granularity.

Who calls `set()`:

- HTTP, until the priming `anext()` returns: the route, which listens on
  `receive()` for `http.disconnect` meanwhile. The request body has been
  read by then, so `receive()` waits until the client goes away, and every
  ASGI spec version requires the server to return `http.disconnect` from
  that `receive()`, whether or not Starlette listens itself. The route then returns a `499` that is
  never delivered. Starting the response before the first chunk instead,
  with the priming moved into the body, would have let Starlette listen,
  but a full queue or a pre-audio `EngineError` would then be a started
  `200`, not a `503` or `400`.
- HTTP, once the response has started: the body's `finally`. Under ASGI
  spec 2.4 Starlette does not listen for `http.disconnect` while streaming,
  so the body learns of a hang-up only when a send fails, at the next
  chunk; uvicorn reports spec 2.3.
- Wyoming, while `handle` waits (a plain `Synthesize`, or a stream's
  remaining sentences after `SynthesizeStop`):
  `VoicedEventHandler.on_close` reads the next event ahead, with no idle
  bound, and reacts to a close, a reset, or an event left half sent for
  `server.session_idle_s` (a client as good as gone). An event it reads is the next one
  handled, so pipelined events keep their order, and the idle bound on it
  still runs from the end of the request.
- Wyoming, while a stream's text is still arriving: the loop reads the close
  and the handler's `disconnect()` closes the stream instead of cancelling
  its worker.

On Wyoming and over HTTP, before the first audio as after it, the sentence
being spoken or waiting for the slot is counted as a `disconnect` (a
waiting one without any engine call), and later sentences are dropped.
