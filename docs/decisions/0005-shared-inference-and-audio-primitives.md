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
health rules wrong.

## Decision

Four small modules own that policy; transports only translate.

| Module | Owns |
|---|---|
| `voiced.inference` | `inference(ctx, operation)`, `classify(exc)`, `record_exception(...)`. |
| `voiced.metrics` | `Outcome`, `Metrics.record(...)`, `Metrics.snapshot()`. |
| `voiced.threads` | `run_blocking(fn, ...)`: a thread call whose cancellation waits for the thread. |
| `voiced.audio` | `PCMConverter`, `pcm16_to_float32`, `float32_to_pcm16`, `wav_header`. |
| `voiced.api.context` | `ServiceContext`, `get_context`, `Ctx`, `APIError`, `require_api_key`, `require_loaded`. |

### Outcome taxonomy

| Outcome | Raised | Health | Metrics | HTTP | Wyoming |
|---|---|---|---|---|---|
| `ok` | nothing | `record_success(ms)` | latency sample | 200 | result event |
| `busy` | `QueueFull` | untouched | count + error ring | 503 + `Retry-After` | `Error`, disconnect |
| `client_error` | `EngineError` | untouched | count + error ring | 400 | `Error`, keep connection |
| `disconnect` | `ConnectionError`, `IncompleteReadError` | untouched | count only | none (peer gone) | none (peer gone) |
| `failure` | any other `Exception` | `record_error` (degraded) | count + error ring | 500 | `Error`, disconnect |

`client_error` leaves health alone because `voiced.engines.base` defines an
`EngineError` as the client's fault; a stream of bad requests must not make
the watchdog restart a healthy process. `disconnect` is deliberately narrow:
an engine that cannot open its weights raises `FileNotFoundError`, an
`OSError`, and that is a failure, not a hang-up. Disconnects are logged at
debug and kept out of the error ring.

`voiced.audio.UnsupportedAudioFormat` subclasses `EngineError`, so a bad
sample width or rate is a client error on every transport without extra code.

### Blocking calls and cancellation

`asyncio.to_thread` gives control back the moment the caller is cancelled,
while the thread keeps running. Inside a slot that frees the slot during GPU
work, and the next request starts a second MLX call concurrently.
`voiced.threads.run_blocking` shields the thread and, on cancellation, waits
for it to finish before re-raising. Engines must use it for every blocking
model call, and `InferenceQueue.run` uses it. A cancelled client therefore
holds its slot until the model is idle again.

### Timing

`inference()` times the block while the slot is held, excluding the wait for
the slot. For streaming (an STT session, a TTS sentence loop) the block is the
whole stream, so `last_inference_ms` and the p50/p95 are full-session numbers.
Time to first audio is a benchmark concern and is measured by the benchmark,
not by the server.

### Cancellation

`CancelledError`, and `GeneratorExit` when a streaming response generator is
closed early, pass through `inference()` untouched: nothing is recorded and
the slot is released by `InferenceQueue.slot()`'s `finally`.

### Rules for transports

- A transport may not catch `QueueFull`, `EngineError` or `Exception` to
  decide what happened. It catches once, calls `classify(exc)` (or
  `record_exception(...)` if the exception may not have passed through an
  `inference()` block), and maps the `Outcome` to its wire format.
- All engine calls happen inside `async with inference(ctx, op):`. Nothing
  outside it calls `ctx.health.record_*` or `ctx.metrics.record`.
- `record_exception` is idempotent per exception object, so a last-resort
  handler (the HTTP 500 handler, the Wyoming handler base) can call it on
  anything it sees without double counting. Exceptions raised outside an
  `inference()` block are recorded under the request path or event name.
- Streaming HTTP responses must surface `QueueFull` and pre-audio
  `EngineError` before the response starts. Write the body as an async
  generator wrapping `async with inference(...)` and prime it with one
  `anext()` in the route, so those exceptions still reach the handlers.
- While holding a slot, every socket await is bounded: reads by
  `server.session_idle_s` (default 30) and writes by `server.write_timeout_s`
  (default 10). A half-open peer otherwise pins the GPU for every other
  client. A timeout is raised as `ConnectionError` (`disconnect`) or as the
  transport's own close; it is never a `failure`.
- Transports do not convert PCM themselves; they use `voiced.audio`.

### HTTP specifics

- `voiced.api.app` registers one handler per exception class: `APIError`
  (its own status), `QueueFull` (503, `Retry-After: 1`), `EngineError` (400),
  `RequestValidationError` (400, matching OpenAI so the `openai` client raises
  `BadRequestError` instead of a generic 422 error), and `Exception` (500).
  All use `{"error": {"message", "type"}}`. On a WebSocket they close the
  socket instead: 1008 for 4xx, 1013 for 503, 1011 for 5xx.
- `require_api_key` takes an `HTTPConnection`, so the same dependency guards
  HTTP routes and WebSocket handshakes.
- Route modules import from `voiced.api.context`, never from `voiced.api.app`,
  so `app.py` imports routes at module level. No function-local imports.

### Startup: bind first, load in the background

The lifespan starts `engine.load()` as a background task and yields at once,
so uvicorn binds immediately. Until the load finishes, `/health` answers 503
with `status: "starting"` (schema unchanged) and every `/v1` route answers
503 with type `starting` (`require_loaded`). If `load()` raises, the process
logs the traceback and exits with status 1, so launchd restarts it. Before
this, `/health` refused connections during a long model load, which the
watchdog could not tell apart from a crash.

`HealthState.snapshot(queue_depth)` takes the depth from the queue, so
`GET /health` reads state and never writes it.

## Consequences

- A new transport (or route) is a mapping table from `Outcome` to its wire
  format plus its protocol parsing; it cannot get health or metrics wrong.
- Metrics are keyed by operation name, not route or status code, so the
  status page shows STT and TTS the same way whichever transport served them.
- Wyoming listeners should also refuse work until `ctx.health.model_loaded`;
  the HTTP side enforces it with `require_loaded`.
