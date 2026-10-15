# 0015: Integration results

Date: 2026-10-15. Status: accepted.

## Context

Until now each layer was validated on its own: the HTTP and Wyoming transports
against fake engines, each engine against its own weights. Nothing had run a
real client clip through the HTTP or Wyoming layer into a real engine and back.
This record covers the end-to-end tier that does, the benchmark that measures
the assembled server against the acceptance targets, what they measured, and
the defects the work exposed.

## Decision

- The end-to-end tier lives in `tests/e2e/` as model-tier tests
  (`@pytest.mark.model`), running the services in-process under uvicorn with
  the same helpers `voiced serve` uses.
- The benchmark is a standalone script, `scripts/bench.py`, not a `voiced
  bench` subcommand. Benchmarking is a developer tool run against servers
  someone else started; keeping it out of the CLI keeps its measurement code
  and the `openai` client off the daemon's path, and it never starts or stops a
  server.
- Memory is reported as the physical footprint (`ri_phys_footprint` from
  `proc_pid_rusage`, what Activity Monitor, `footprint` and memory pressure
  count), not RSS: MLX's Metal buffers are in the footprint and mostly not in
  the resident set. Each service reports it in `/metrics.json`, with RSS and
  MLX's active, cached and peak allocation beside it, and the benchmark reads
  it from there.

## The end-to-end tier

`tests/e2e/` starts the real `stt` and `tts` services the way `voiced serve`
does: the same config loader (`load_settings` over a TOML file), the same
logging setup and app factory (`voiced.cli.build_service_app`, Wyoming listener
attached), the same uvicorn settings (`voiced.cli.uvicorn_config`) and the
configured real engine, each on an ephemeral HTTP port and an ephemeral Wyoming
port. The tests skip unless `VOICED_MODEL_TESTS=1`, so CI never loads MLX. They
cover:

- the unmodified `openai` client: `audio.transcriptions.create` in every
  response format (json, text, verbose_json, srt cues, vtt with its header) and
  `audio.speech.create` for Kokoro and Soprano, streamed and non-streamed, in
  every format (wav, pcm, mp3, opus); the synthesized audio is fed back through
  the STT service and must contain the words (Kokoro wav; Soprano wav, mp3 and
  opus, which covers its 32 kHz to 24 kHz resample and the encoders);
- WebSocket streaming: real-time 64 ms and 100 ms chunks, partials that arrive
  before `end`, and the finalize latency from the client's `end` message to the
  `final` event, under 150 ms with 200 ms and with 0 ms of trailing silence;
- Wyoming: the `wyoming` client library round trip for STT (`Transcribe`,
  `AudioStart`/`AudioChunk`/`AudioStop`, `Transcript`, also without
  `AudioStart`) and TTS (plain `Synthesize` and the streaming
  `SynthesizeStart`/`SynthesizeChunk`/`SynthesizeStop` shape with the
  compatibility `Synthesize` Home Assistant sends mid-stream, whose audio is
  transcribed to show the text is spoken once);
- concurrency: several simultaneous clients, the busy response when the queue
  is full (HTTP batch 503, streamed speech 503 before any audio, Wyoming
  `Error(server_busy)`), a client that hangs up mid-synthesis, the
  `session_idle_s` and `write_timeout_s` bounds that free the slot, and the STT
  finalize and idle bounds while Kokoro synthesizes continuously on the same
  GPU;
- lifecycle: `/health` 503 until warm then 200, graceful shutdown finishing a
  streaming synthesis within `GRACEFUL_SHUTDOWN_S`. A start on a used port is
  tested through the real `voiced serve` in a subprocess
  (`tests/cli/test_serve.py`, no model needed, so it runs on CI too).

Run it with:

```
VOICED_MODEL_TESTS=1 uv run pytest tests/e2e -q
```

## How the table was produced

Three services on non-default ports so they cannot collide with an installed
service, then the benchmark:

```
export VOICED_CONFIG=/nonexistent.toml VOICED_SERVER__ZEROCONF=false VOICED_SERVER__BIND=127.0.0.1
VOICED_STT__HTTP_PORT=18881 VOICED_STT__WYOMING_PORT=18882 uv run voiced serve stt &
VOICED_TTS__HTTP_PORT=18880 VOICED_TTS__WYOMING_PORT=18883 uv run voiced serve tts &
VOICED_TTS__ENGINE=soprano VOICED_TTS__MODEL=mlx-community/Soprano-80M-bf16 \
  VOICED_TTS__DEFAULT_VOICE=default VOICED_TTS__HTTP_PORT=18870 VOICED_TTS__WYOMING_PORT=18871 \
  uv run voiced serve tts &

uv run python scripts/bench.py \
  --stt-url http://127.0.0.1:18881 --tts-url http://127.0.0.1:18880 \
  --soprano-url http://127.0.0.1:18870 --alignments 32 --trials 10
```

`uv run python scripts/bench.py --help` documents each argument and the
`LLM_BASE_URL`, `LLM_API_KEY` and `LLM_MODEL` variables; the script's
docstring defines each row.

## Results

Measured on the development machine: Apple M1 Max, 64 GB, macOS 15.7.4, warm
models, loopback requests, each service in its own process, with the command
above. "First audio" is from sending the request with the `openai` client
(streamed, `pcm`) to the first byte of audio, for the single sentence "Turn on
the kitchen lights."; "finalize" is from sending `end` to the `final` event.

| Metric | Measured | Target |
|---|---|---|
| Kokoro first audio | median 96 / p95 107 / worst 112 ms (n=10) | <= 200 ms |
| Soprano first audio | median 156 / p95 190 / worst 195 ms (n=10) | none (off by default) |
| Streaming finalize, 64 ms chunks, 200 ms trailing silence | median 1 / p95 2 / worst 3 ms (n=32) | <= 150 ms worst, 50 ms median |
| Streaming finalize, 64 ms chunks, 0 ms trailing silence | median 114 / p95 130 / worst 141 ms (n=32) | <= 150 ms worst, 50 ms median |
| Streaming finalize, 100 ms chunks, 200 ms trailing silence | median 1 / p95 4 / worst 16 ms (n=32) | <= 150 ms worst, 50 ms median |
| Streaming finalize, 100 ms chunks, 0 ms trailing silence | median 114 / p95 127 / worst 130 ms (n=32) | <= 150 ms worst, 50 ms median |
| Streaming finalize, 64 ms chunks, 200 ms trailing silence, Kokoro synthesizing | median 1 / p95 49 / worst 54 ms (n=16) | <= 150 ms worst |
| Streaming finalize, 64 ms chunks, 0 ms trailing silence, Kokoro synthesizing | median 140 / p95 181 / worst 192 ms (n=16) | <= 150 ms worst |
| Batch transcription, 10.1 s clip | median 253 / p95 258 / worst 258 ms (n=10) | <= 300 ms |
| STT memory | footprint 1.47 GB; RSS 0.37 GB; MLX active 1296 MB, cache 0 MB, peak 3983 MB | |
| TTS (Kokoro) memory | footprint 1.00 GB; RSS 0.53 GB; MLX active 342 MB, cache 542 MB, peak 2159 MB | |
| TTS (Soprano) memory | footprint 0.40 GB; RSS 0.44 GB; MLX active 217 MB, cache 10 MB, peak 283 MB | |
| STT + TTS (Kokoro) footprint, idle after use | 2.47 GB | <= 3 GB |
| STT -> LLM -> TTS loop | not measured (no LLM endpoint configured for this run) | none |

Notes on the rows:

- The finalize targets hold when the client streams the endpointer's 200 ms
  of trailing silence before `end` (ADR 0008's definition): the engine's
  speculative final encode has then already run, and finalize is 1 to 16 ms.
  A push-to-talk client that sends `end` at the last word makes finalize encode
  the tail after `end`: about 114 ms median, under 150 ms worst on its own, but
  not with the 50 ms median goal (see known misses).
- The batch row is 90 ms above ADR 0008's 163 ms for a 9.4 s clip because it
  is the whole request: a multipart upload through the `openai` client, the
  ffmpeg decode of the WAV, and a 10.1 s clip (the 3.37 s fixture tiled three
  times). The margin to the 300 ms target is about 42 ms.
- Memory is each service's own `/metrics.json`, read 1 s after a long reply
  (TTS) or a batch of 10 s clips (STT); `footprint -p` agreed within 5%. The
  STT MLX peak is its load (fp32 weights cast to bf16, ADR 0008), not serving.
  The Kokoro MLX peak of 2.16 GB is reached during the four-sentence reply;
  ADR 0009's figures for Kokoro alone are 469 MB maximum RSS and 326 MB active
  MLX memory, which RSS-based reporting understated.
- The loop row was not re-measured for this record. An earlier run against a
  small local reasoning model gave STT 3 + LLM 2114 + TTS 103 = 2210 ms
  median; that version of the script silently spoke a fallback when the reply
  was only hidden thinking, so the LLM figure may include replies that were
  never spoken. The script now counts and prints such fallbacks.

## Defects found and fixed

1. **`voiced install` would fail after the first PyPI release.**
   `InstallPlan.requirement` built `{source}[extras]`, so the release default
   `voiced==X` became `voiced==X[stt,tts]`, which is not valid PEP 508 and pip
   rejects. Extras now go before the version (`voiced[stt,tts]==X`), with a path
   or bare-name source still taking them at the end
   (`requirement_with_extras`). Regression tests:
   `tests/launchd/test_install.py::test_requirement_places_extras_before_a_version_specifier`
   and `::test_requirement_with_extras_variants`, plus the corrected
   expectations in `test_venv_commands_with_uv` and
   `test_cli_dry_run_prints_everything_and_writes_nothing`, which had encoded
   the wrong string.

2. **The `finalize` docstring misdescribed who ends a turn.** The docstring on
   `STTStreamSession.finalize` (and the matching `voiced.vad` row in ADR 0001)
   said the server's endpointer calls it. It does not: the streaming WebSocket
   client's `end` message and the Wyoming `AudioStop` call it; the engine's own
   endpointer only decides the speculative final encode inside the session.
   Wording corrected in both; no signature changed (the interface stays frozen).

3. **Kokoro's MLX cache grew with every reply and was never released.**
   Symptom: the TTS service's footprint was 2.2 GB after warm-up, 3.0 GB after
   one short reply and 4.7 GB after one 70-word reply, held while idle; STT plus
   TTS was 4.4 to 6.1 GB against the 3 GB budget. In-process, Kokoro held 10.2
   GB of MLX cache after the four-sentence reply in the regression test. RSS,
   which the first version of the benchmark reported, showed 0.5 GB. Cause: MLX
   keeps freed buffers in a cache for reuse; a sentence-at-a-time engine
   allocates differently shaped buffers for every sentence, so the cache only
   grows, and nothing in the TTS engines bounded it (Parakeet clears its own).
   Fix: `SentenceTTSEngine.load()` sets `mx.set_cache_limit(512 MB)` and empties
   the cache the warm-up left; Soprano shares the base class. Clearing the
   cache after every sentence was measured too: it keeps the cache at zero
   (Kokoro footprint about 0.7 GB) but every sentence then allocates afresh,
   which put Kokoro's first audio at 118 ms against 94 ms unbounded; with the
   512 MB limit it is 102 ms at about 1.0 GB. Regression tests:
   `tests/tts/test_sentence_engines.py::test_load_bounds_the_mlx_cache_even_when_it_fails`
   (CI) and `::test_synthesis_keeps_the_mlx_cache_bounded` (model tier: the
   limit is set and `mx.get_cache_memory()` stays within 1.25 times it after a
   long synthesis; 10.2 GB without the fix).

4. **A port clash exited 0, or only after a full model load.** Symptom: with
   the HTTP port taken, uvicorn logged "address already in use", shut the
   lifespan down while the engine was still loading, and the process exited 0
   ("shutdown during engine load"), which launchd treats as a clean stop. With
   the Wyoming port taken, the listener failed inside lifespan startup while the
   load thread ran; the process exited 3 only after loading and warming the
   model. Cause: uvicorn binds its socket after the lifespan has started the
   load, and nothing stopped a load once startup failed. Fix: `voiced serve`
   binds the HTTP socket itself and checks the Wyoming port before it builds
   the app, and on a clash prints `HTTP port N on HOST is already in use ...`
   and exits 3 before anything loads; uvicorn serves on the pre-bound socket.
   If startup still fails while the engine loads (the Wyoming port taken in the
   moment between), the lifespan exits 3 at once instead of waiting for the
   load. Regression tests: `tests/cli/test_serve.py` (the real `voiced serve` in
   a subprocess, each service, each port held; exits 3 within 5 s with the port
   named and no log file opened) and
   `tests/core/test_api_context.py::test_startup_failure_during_load_exits_non_zero_without_waiting`.

5. **Selecting Soprano by `engine` alone fails at load with an
   `AttributeError`** (usability defect, not fixed here). Symptom: with
   `engine = "soprano"` and the default `model` and `default_voice`, the TTS
   service exits during load with `AttributeError: 'ModelConfig' object has no
   attribute 'decoder_config'`. Cause: the engine's model and voice defaults are
   not tied to the engine; the defaults are Kokoro's, so Soprano's code loads
   Kokoro's weights. Three keys must change together, and only the README says
   so. Status: `voiced settings set tts.engine=soprano` (and the status page
   form) now fill in the engine's own model and voice (ADR 0013), but a hand
   edited config or a `VOICED_TTS__ENGINE` override still reaches the load. A
   load-time check that names the mismatch is left to the engine work.

## Known misses

- **Push-to-talk finalize median.** With 0 ms of trailing silence the median
  is about 114 ms against the 50 ms median goal, and the worst of 32 has
  reached 165 ms in an earlier run on the same machine. Cause: the tail must be
  encoded after `end`; the target as defined in ADR 0008 assumes the
  endpointer's silence window. Clients that can should stream 200 ms of
  silence (or keep the microphone open that long) before `end`.
- **Push-to-talk finalize while TTS synthesizes.** With Kokoro synthesizing
  continuously in the other process, 0 ms finalize is 140 ms median and up to
  192 ms (n=16; the in-process e2e test saw up to 243 ms). The tail encode
  waits behind Kokoro's work on the one GPU. With 200 ms of trailing silence
  it stays at 54 ms worst. Not addressed here: it needs GPU scheduling between
  the two services.
- **Transient memory during a long reply.** The idle footprint after use is
  2.47 GB, within the 3 GB budget, but Kokoro's MLX allocation reaches 2.16 GB
  while it synthesizes a long sentence, so STT plus TTS can briefly exceed 3
  GB. The STT load peak (about 4 GB) is a one-off at start (ADR 0008).

## Versions

voiced 0.0.1 at this commit, mlx 0.32.3, parakeet-mlx 0.5.3, mlx-audio 0.5.9,
openai 3.31.0, wyoming 1.10.2, uvicorn 0.54.0, Python 3.12, macOS 15.7.4.

## Consequences

- The e2e tier is the regression net for the transport/engine seam; it is a
  model-tier suite, so CI (no MLX) still skips it cleanly. Its service fixtures
  are package-scoped and release each engine's MLX memory on teardown (drop the
  model, stop its thread with `ModelThread.shutdown()`, `mx.clear_cache()`), so
  running the whole model tier in one process does not leave three extra
  models resident and inflate the Parakeet `mx.get_peak_memory()` check in
  `tests/stt`.
- `/metrics.json` and the status page report the footprint, so a cache that
  grows again is visible without a profiler.
- The 3 GB budget exists because the target Mac also keeps an LLM resident
  (operations.md, memory). A second machine is not yet measured; the README
  column for it says so.
