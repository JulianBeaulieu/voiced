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
| `disconnect` | `DISCONNECT_ERRORS` | untouched | count only, debug log | none (peer gone) | none (peer gone) |
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
  `EngineError` before the response starts: write the body as an async
  generator wrapping `async with inference(...)` and prime it with one
  `anext()` in the route.
- **Never nest `inference()` in one task.** The second call would wait for
  the slot its own task holds; `slot()` raises `RuntimeError` instead.
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
the slot. For streaming (an STT session, a TTS sentence loop) the block is the
whole stream, so `last_inference_ms` and the p50/p95 are full-session numbers.
Time to first audio is a benchmark concern and is measured by the benchmark,
not by the server.

### HTTP specifics

- `voiced.api.app` has one handler for `QueueFull` and `EngineError` that
  reads `_HTTP[classify(exc)]`, plus handlers for `APIError` (its own
  status), `RequestValidationError` (400, as OpenAI does, so the `openai`
  client raises `BadRequestError` instead of a generic 422 error) and
  `Exception` (500; marks health degraded unless the exception is a
  disconnect). All use `{"error": {"message", "type"}}`; `/v1` returns type
  `starting` while the engine loads.
- Starlette runs these handlers for errors raised during a WebSocket
  handshake (the API-key dependency) but never for errors after `accept()`,
  and its 500 handler is HTTP-only. A WebSocket route therefore follows the
  transport rule above with its own close-code table. A close before
  `accept()` reaches the client as HTTP 403, not as close code 1008.
- `require_api_key` takes an `HTTPConnection`, so the same dependency guards
  HTTP routes and WebSocket handshakes, and compares with
  `hmac.compare_digest`.
- Route modules import from `voiced.api.context` and `voiced.context`, never
  from `voiced.api.app`, so `app.py` imports routes at module level.

### Startup: bind first, load in the background

The lifespan starts `engine.load()` as a background task and yields at once,
so uvicorn binds immediately. Until the load finishes, `/health` answers 503
with `status: "starting"` (schema unchanged) and `/v1` answers 503 through
`check_loaded()`. This only works if `load()` does its blocking work through
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
  bug in a route) never reach metrics. Only an unexpected exception touches
  health, through the HTTP 500 handler.
