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
  `final` event: under 150 ms with 200 ms of trailing silence; with 0 ms, under
  a regression ceiling only (see known misses);
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
docstring defines each row. Against servers that set `server.api_key` (any server
bound beyond loopback), pass the key with `--api-key` or `VOICED_API_KEY`; the
table's footer says `auth: key` or `auth: none`, never the key.

## Results

Measured on the development machine: Apple M1 Max, 64 GB, macOS 15.7.4, warm
models, loopback requests, each service in its own process, with the command
above. The services were started from a terminal (`uv run voiced serve`), not
by launchd as an installed service runs them; under launchd the short bursts
were slower until the fix in [Under launchd](#under-launchd). "First audio" is
from sending the request with the `openai` client (streamed, `pcm`) to the
first byte of audio, for the single sentence "Turn on the kitchen lights.";
"finalize" is from sending `end` to the `final` event.

| Metric | Measured | Target |
|---|---|---|
| Kokoro first audio | median 96 / p95 107 / worst 112 ms (n=10) | <= 200 ms |
| Soprano first audio | median 156 / p95 190 / worst 195 ms (n=10) | none (off by default) |
| Streaming finalize, 64 ms chunks, 200 ms trailing silence | median 1 / p95 2 / worst 3 ms (n=32) | <= 150 ms worst, 50 ms median |
| Streaming finalize, 64 ms chunks, 0 ms trailing silence | median 114 / p95 130 / worst 141 ms (n=32) | <= 150 ms worst, 50 ms median (miss, see known misses) |
| Streaming finalize, 100 ms chunks, 200 ms trailing silence | median 1 / p95 4 / worst 16 ms (n=32) | <= 150 ms worst, 50 ms median |
| Streaming finalize, 100 ms chunks, 0 ms trailing silence | median 114 / p95 127 / worst 130 ms (n=32) | <= 150 ms worst, 50 ms median (miss, see known misses) |
| Streaming finalize, 64 ms chunks, 200 ms trailing silence, Kokoro synthesizing | median 1 / p95 49 / worst 54 ms (n=16) | <= 150 ms worst |
| Streaming finalize, 64 ms chunks, 0 ms trailing silence, Kokoro synthesizing | median 140 / p95 181 / worst 192 ms (n=16) | <= 150 ms worst (miss, see known misses) |
| Batch transcription, 10.1 s clip | median 253 / p95 258 / worst 258 ms (n=10) | <= 300 ms |
| STT memory | footprint 1.47 GB; RSS 0.37 GB; MLX active 1296 MB, cache 0 MB, peak 3983 MB | |
| TTS (Kokoro) memory | footprint 0.74 GB; RSS 0.53 GB; MLX active 342 MB, cache 279 MB, peak 2159 MB | |
| TTS (Soprano) memory | footprint 0.40 GB; RSS 0.44 GB; MLX active 217 MB, cache 10 MB, peak 283 MB | |
| STT + TTS (Kokoro) footprint, idle after use | 2.21 GB | <= 3 GB |
| STT -> LLM -> TTS loop | not measured (no LLM endpoint configured for this run) | none |

Notes on the rows:

- The finalize targets hold when the client streams the endpointer's 200 ms
  of trailing silence before `end` (ADR 0008's definition): the engine's
  speculative final encode has then already run, and finalize is 1 to 16 ms.
  A push-to-talk client that sends `end` at the last word makes finalize encode
  the tail after `end`: about 114 ms median and usually under 150 ms worst on
  its own (141 ms in this run, 165 ms in an earlier one), which misses the
  50 ms median goal and does not reliably meet the 150 ms worst case (see known
  misses).
- The batch row is 90 ms above ADR 0008's 163 ms for a 9.4 s clip because it
  is the whole request: a multipart upload through the `openai` client, the
  ffmpeg decode of the WAV, and a 10.1 s clip (the 3.37 s fixture tiled three
  times). The margin to the 300 ms target is about 42 ms.
- The Kokoro memory row and the combined footprint were re-measured with the
  same command on the final code, with `main`'s 256 MB MLX cache cap (defect
  3). On that run the other rows matched within noise, except Soprano's tail
  (median 149 / p95 272 / worst 356 ms; Soprano is off by default and has no
  target).
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
   Fix: `SentenceTTSEngine.load()` sets `mx.set_cache_limit(256 MB)` and empties
   the cache the warm-up left; Soprano shares the base class. Clearing the
   cache after every sentence was measured too: it keeps the cache at zero
   (Kokoro footprint about 0.7 GB) but every sentence then allocates afresh,
   which put Kokoro's first audio at 118 ms against 94 ms unbounded; with the
   512 MB limit first measured here it was 102 ms at about 1.0 GB. The limit
   shipped is 256 MB: first audio within 0 to 3 ms of an unbounded cache and
   Kokoro's footprint 0.74 GB (the table above). Regression tests:
   `tests/tts/test_sentence_engines.py::test_load_bounds_the_mlx_cache_even_when_it_fails`
   (CI) and `::test_synthesis_keeps_the_mlx_cache_bounded` (model tier: the
   limit is set and `mx.get_cache_memory()` stays within 1.25 times it after a
   long synthesis; 10.2 GB without the fix). The fix and its tests landed on
   `main` separately, in PR #16, ahead of this record; the measurements here
   were taken with it.

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
  endpointer's silence window. This is the same encode as ADR 0008's "At
  finalize" column (120 to 133 ms worst at 3.4 s, measured on the engine
  alone), which that record rejected as the default for the same reason. Clients that can should stream 200 ms of
  silence (or keep the microphone open that long) before `end`.
- **Push-to-talk finalize while TTS synthesizes.** With Kokoro synthesizing
  continuously in the other process, 0 ms finalize is 140 ms median and up to
  192 ms (n=16; the in-process e2e test saw up to 243 ms). The tail encode
  waits behind Kokoro's work on the one GPU. With 200 ms of trailing silence
  it stays at 54 ms worst. Not addressed here: it needs GPU scheduling between
  the two services. **Closed by ADR 0019** (Kokoro waits while an STT
  encoder call a client waits on runs): on the Mac Studio the row is median
  125 to 130 / worst 136 to 149 ms, within the 150 ms worst case, against
  159 to 164 / 203 to 204 on main (see [With the GPU gate](#with-the-gpu-gate)).
- **Kokoro first audio while another client's utterance ends.** With the
  GPU gate (ADR 0019), first audio while another client streams to the STT
  is median 128 to 130 / p95 158 to 200 / worst 239 to 243 ms on the Mac
  Studio (n=30 per round), against 116 to 118 / 141 to 143 / 149 to 156 on
  main; the target is 200 ms. Only the top few percent go over: the
  requests that land while the other client's urgent tail runs (its call at
  a pause and its last calls, 100 to 150 ms each), which the reply waits
  for, up to 300 ms per sentence. Both clients wait on the one GPU at the
  same moment, and the STT's push-to-talk finalize, the owner's priority,
  is given it. Two other gate designs were measured on the Studio and
  rejected: every encoder call taking the lock (first audio worst 252 to
  265 ms) and a 45 ms budget for a reply's first sentence (first audio
  within target, but push-to-talk finalize back to worst 181 to 210 ms).
- **Transient memory while serving.** The idle footprint after use is
  2.21 GB, within the 3 GB budget, but both engines peak above their idle
  allocation while they work: Kokoro's MLX allocation reaches 2.16 GB during
  the four-sentence reply (0.62 GB idle), and Parakeet's serving peak is
  2.46 GB (1.28 GB after load, ADR 0008; reached on long input). Either one
  alone takes STT plus TTS over 3 GB for a moment. If they coincide, the MLX peaks
  alone add to 4.6 GB, so the two footprints together would be about 4.9 GB;
  that is an estimate from the separate peaks, not a measurement. The STT load
  peak (about 4 GB) is a one-off at start (ADR 0008).

## Mac Studio

Measured on the target machine: Mac Studio, M1 Max with the 24-core GPU,
32 GB, macOS 27.0.1, voiced `main` at 48e1f20, before the services set the
foreground task role (see [With the foreground task role](#with-the-foreground-task-role)
for an A/B of that fix). The services ran as installed
LaunchDaemons with the 256 MB Kokoro cache cap, and Ollama kept
`ministral-3:8b` resident on the same GPU. `scripts/bench.py` ran with its
defaults (10 trials, 32 alignments) and the servers' API key, on the Studio
over loopback; Soprano was not running.

| Metric | Measured | Target |
|---|---|---|
| Kokoro first audio | median 85 / p95 88 / worst 89 ms (n=10) | <= 200 ms |
| Streaming finalize, 64 ms chunks, 200 ms trailing silence | median 3 / p95 42 / worst 48 ms (n=32) | <= 150 ms worst |
| Streaming finalize, 64 ms chunks, 0 ms trailing silence | median 149 / p95 160 / worst 164 ms (n=32) | <= 150 ms worst (miss) |
| Streaming finalize, 100 ms chunks, 200 ms trailing silence | median 3 / p95 5 / worst 59 ms (n=32) | <= 150 ms worst |
| Streaming finalize, 100 ms chunks, 0 ms trailing silence | median 146 / p95 158 / worst 170 ms (n=32) | <= 150 ms worst (miss) |
| Streaming finalize, 64 ms chunks, 200 ms trailing silence, Kokoro synthesizing | median 179 / p95 236 / worst 250 ms (n=16) | <= 150 ms worst (miss) |
| Streaming finalize, 64 ms chunks, 0 ms trailing silence, Kokoro synthesizing | median 366 / p95 461 / worst 510 ms (n=16) | <= 150 ms worst (miss) |
| Batch transcription, 10.1 s clip | median 258 / p95 273 / worst 275 ms (n=10) | <= 300 ms |
| STT memory | footprint 1.61 GB; RSS 0.46 GB; MLX active 1296 MB, cache 0 MB, peak 3727 MB | |
| TTS (Kokoro) memory | footprint 0.75 GB; RSS 0.51 GB; MLX active 342 MB, cache 279 MB, peak 2159 MB | |
| STT + TTS (Kokoro) footprint, idle after use | 2.36 GB | <= 3 GB |
| STT -> LLM -> TTS loop, end of speech to first audio (median), from a laptop over Wi-Fi | STT 7 + LLM 217 + TTS 221 = 436 ms | none |

The loop row comes from a second run of the same script on an M1 Max laptop
over Wi-Fi to the Studio, with `LLM_BASE_URL` pointing at the Studio's Ollama
and `LLM_MODEL=ministral-3:8b`. Over Wi-Fi, batch transcription was 326 ms
median (412 ms worst), about 70 ms of upload over loopback's 258 ms, and
Kokoro first audio 105 ms median, about 20 ms more.

Misses on the Studio, before the task role fix:

- **Push-to-talk finalize.** With 0 ms of trailing silence the worst of 32
  is 164 ms (64 ms chunks) and 170 ms (100 ms chunks), medians 146 to 149 ms:
  the same miss as on the development machine (see known misses), somewhat
  slower.
- **GPU contention was much worse than on the development machine in this
  run.** With Kokoro synthesizing in the other process, finalize after 200 ms
  of trailing silence is median 179 / worst 250 ms (development machine:
  median 1, worst 54), and with 0 ms median 366 / worst 510 ms (140 / 192).
  The two later runs without the task role, at 02c6f30 under the same
  conditions, did not reproduce it (see [With the foreground task role](#with-the-foreground-task-role));
  what made this run slower was not isolated. Part of the gap to the
  development machine is launchd, not the Studio: the development machine's
  numbers came from a terminal, and the same code under launchd on the
  development machine is slower too (see [Under launchd](#under-launchd)); a
  resident Ollama model was ruled out there. It matters when one satellite is
  speaking while another's reply is synthesized; a single Home Assistant
  pipeline does not overlap STT and TTS.
- The idle footprint after use is 2.36 GB, within the 3 GB budget.

### With the foreground task role

The fix in [Under launchd](#under-launchd), measured on the Studio as an A/B:
02c6f30 (no task role) and eda0ae5 (the role), alternating A, B, A, B, back
to back. Each run is `scripts/bench.py` with its defaults over loopback
against the installed LaunchDaemons, deployed with `voiced install --source`
and `voiced restart all`, both services healthy before each run, Ollama
keeping `ministral-3:8b` resident throughout. Each cell is the range across
the two runs. With the role, both services logged
`task role set to foreground application (darwin role read back: 1)` at
start, so the role is set in the system domain on macOS 27; the `ENOTSUP`
refusal applies to `setpriority`, not to the `task_policy_set` call used.

| Metric | No role (02c6f30) | Role (eda0ae5) | Target |
|---|---|---|---|
| Kokoro first audio | median 85 / p95 88 / worst 89 ms | median 85 to 86 / p95 87 to 88 / worst 88 to 89 ms | <= 200 ms |
| Streaming finalize, 64 ms chunks, 200 ms trailing silence | median 3 / p95 49 to 56 / worst 63 to 74 ms | median 3 / p95 3 to 4 / worst 5 ms | <= 150 ms worst |
| Streaming finalize, 64 ms chunks, 0 ms trailing silence | median 145 to 148 / p95 157 to 160 / worst 161 to 165 ms | median 122 to 123 / p95 130 / worst 136 to 137 ms | <= 150 ms worst |
| Streaming finalize, 100 ms chunks, 200 ms trailing silence | median 3 / p95 4 / worst 34 to 40 ms | median 3 / p95 4 / worst 12 to 32 ms | <= 150 ms worst |
| Streaming finalize, 100 ms chunks, 0 ms trailing silence | median 147 to 148 / p95 158 to 163 / worst 163 to 165 ms | median 123 to 125 / p95 132 / worst 134 to 136 ms | <= 150 ms worst |
| Streaming finalize, 64 ms chunks, 200 ms trailing silence, Kokoro synthesizing | median 3 to 10 / p95 55 to 70 / worst 97 to 98 ms | median 2 / p95 38 to 61 / worst 64 to 75 ms | <= 150 ms worst |
| Streaming finalize, 64 ms chunks, 0 ms trailing silence, Kokoro synthesizing | median 174 to 178 / p95 215 to 218 / worst 229 to 252 ms | median 156 to 167 / p95 184 to 207 / worst 201 to 222 ms | <= 150 ms worst (miss) |
| Batch transcription, 10.1 s clip | median 244 to 246 / p95 249 to 265 / worst 249 to 267 ms | median 254 to 256 / p95 264 to 266 / worst 264 to 268 ms | <= 300 ms |
| STT + TTS (Kokoro) footprint, idle after use | 2.27 GB | 2.26 to 2.27 GB | <= 3 GB |

The finalize rows are n=32 alone and n=16 with Kokoro synthesizing; first
audio and batch are n=10. With the role, push-to-talk finalize alone moves
from just over the 150 ms worst case (161 to 165 ms) to under it (134 to
137 ms), 23 to 25 ms off the median; its median of 122 to 125 ms is still the
50 ms median miss in [Known misses](#known-misses). Finalize after 200 ms of
trailing silence loses its tail of slow cases (worst 5 against 63 to 74 ms
with 64 ms chunks). The one row still over its target is push-to-talk
finalize while Kokoro synthesizes, median 156 to 167 / worst 201 to 222 ms,
against 174 to 178 / 229 to 252 without the role: the GPU contention in
[Known misses](#known-misses). Batch transcription was about 10 ms slower
with the role in both rounds, within its target; two rounds do not show
whether that is the role. Kokoro first audio and memory did not change. The
push-to-talk medians with the role match the development machine's
LaunchAgents after the fix (122 and 124 ms), so on this measurement the
system domain and macOS 27 did not change the result.

### With the GPU gate

ADR 0019 (STT encoder calls go first; Kokoro waits between blocks), measured
on the Studio as an A/B against main 6e6012d, alternating, two rounds,
installed LaunchDaemons, `scripts/bench.py` defaults; each cell is the range
over both rounds. This is the gate as first built (3d522c3: every encoder
call took the lock, one 300 ms wait budget for every sentence); ADR 0019 has
the full table and the final design.

| Metric | main | With the gate | Target |
|---|---|---|---|
| Streaming finalize, 64 ms chunks, 0 ms trailing silence, Kokoro synthesizing | median 152 to 177 / p95 201 to 210 / worst 201 to 221 ms | median 124 to 127 / p95 138 to 141 / worst 141 to 148 ms | <= 150 ms worst |
| Streaming finalize, 64 ms chunks, 200 ms trailing silence, Kokoro synthesizing | median 2 / p95 69 to 76 / worst 97 to 100 ms | median 2 / p95 3 to 15 / worst 3 to 16 ms | <= 150 ms worst |
| Streaming finalize, 64 ms chunks, 0 ms trailing silence | median 123 to 125 / worst 131 to 134 ms | median 125 / worst 138 to 142 ms | <= 150 ms worst |
| Kokoro first audio | median 86 / worst 90 to 97 ms | median 91 / worst 92 to 110 ms | <= 200 ms |
| Kokoro first audio while another client streams to STT | median 116 / worst 148 to 149 ms | median 127 to 129 / worst 252 to 265 ms | <= 200 ms |

First audio while another client speaks went over its target; the final
design takes the lock only for the encoder calls a client waits on (the
tail of an utterance, or a session that is behind). Against main 1145f82,
same setup:

| Metric | main | Final gate (f437a8f) | Target |
|---|---|---|---|
| Streaming finalize, 64 ms chunks, 0 ms trailing silence, Kokoro synthesizing | median 159 to 164 / p95 187 to 202 / worst 203 to 204 ms | median 125 to 130 / p95 132 to 146 / worst 136 to 149 ms | <= 150 ms worst |
| Streaming finalize, 64 ms chunks, 200 ms trailing silence, Kokoro synthesizing | median 2 / p95 56 to 66 / worst 64 to 71 ms | median 2 / p95 3 / worst 3 to 5 ms | <= 150 ms worst |
| Streaming finalize, 64 ms chunks, 0 ms trailing silence | median 123 to 124 / worst 129 to 136 ms | median 124 to 125 / worst 136 ms | <= 150 ms worst |
| Kokoro first audio | median 85 to 86 / worst 90 to 92 ms | median 92 to 93 / worst 113 ms | <= 200 ms |
| Kokoro first audio while another client streams to STT | median 116 to 118 / p95 141 to 143 / worst 149 to 156 ms | median 128 to 130 / p95 158 to 200 / worst 239 to 243 ms | <= 200 ms |

The STT miss is closed. First audio while another client's utterance ends
is a known miss (Known misses).

Self-healing, with the installed LaunchDaemons on 48e1f20:

- `kill -9` of the stt or tts process: launchd's KeepAlive restarts it, and it
  is healthy again after 2.1 to 2.5 s.
- `SIGSTOP` (a hung process): the watchdog restarts it after three failed
  polls (15 s interval, 5 s timeout); stt was healthy again after 45 s, tts
  after 40 s. Before the watchdog's startup grace ended at the first healthy
  poll (48e1f20), a hang within 300 s of a restart took 157 to 180 s.

## Under launchd

An installed service runs as a LaunchDaemon (`ProcessType` Interactive), not
from a terminal, and on the development machine that alone made short,
bursty work slower. Sustained work (the 10 s batch) was unchanged.

**What was measured.** LaunchAgents under `gui/<uid>` carrying the daemons'
plist keys, alternating with a terminal baseline. A microbenchmark (sleep 1 s,
then a 12 ms Python loop, 20 times) took median 13 to 21 ms from a terminal
and 33 to 38 ms in a LaunchAgent; run back to back, without the gap, it took
11 ms in both. So a launchd job runs the first burst of CPU work after an idle
gap far slower than a terminal child does, and the same once warm. A
streaming finalize is that shape: the client's `end` arrives after a gap in
the work, and the tail encode is a short burst.

**What it is attributed to (inferred, not measured).** Both a terminal child
and a launchd job have task role 0 (`TASK_UNSPECIFIED`; the role is not
inherited). What differs is the coalition: a terminal child runs in the
terminal app's coalition, a launchd job in one of its own. Which policy
macOS applies to that coalition, whether a slower core, a lower clock, or a
QoS ceiling on an unspecified task (which would explain why raising the
threads' QoS did nothing), was not observable without root.

**What was tried.** 0 ms finalize alone, 64 ms chunks, median of n=16 per
run, 2 to 3 runs each (this row exercises only the stt service, which
carried the change under test):

| Condition | median ms |
|---|---|
| terminal | 104 to 118 |
| LaunchAgent, `ProcessType` Interactive (as installed) | 145 to 148 |
| + main thread at `QOS_CLASS_USER_INTERACTIVE` | 145 to 148 |
| + every Python thread, model threads included, at `QOS_CLASS_USER_INTERACTIVE` | 143 to 144 |
| + task role `TASK_FOREGROUND_APPLICATION` | 120 to 122 |
| + task role and every thread at `QOS_CLASS_USER_INTERACTIVE` | 120 to 123 |

The brief's other candidates, on the microbenchmark (LaunchAgent 33 to 38 ms
unless noted):

- `ProcessType` Standard or unset: no change. Adaptive: slower (62 ms).
- `Nice` -10: no change.
- Timer coalescing: latency tier 0 and throughput tier 0 (`taskpolicy -l 0
  -t 0`): no change. `LegacyTimers` true: no change (35 to 37 ms).
- `taskpolicy -a` (application resource policies): no change.
- Task roles `TASK_CONTROL_APPLICATION` and `TASK_GRAPHICS_SERVER`: refused
  without privileges. `TASK_DEFAULT_APPLICATION`: no change.
- `LowPriorityIO`: not tested; it is off by default and the finalize path
  does no disk I/O.
- P-core against E-core placement and the GPU performance state: not
  measured; both need `powermetrics`, which needs root. On the
  microbenchmark the first GPU burst after a gap (34 ms warm in both) took
  median 56 to 73 ms from a terminal and 65 to 84 ms in a LaunchAgent: the
  ranges overlap, so no GPU difference is claimed.

**Fix.** `voiced serve` sets its own task role to foreground application at
start (`voiced.cli.request_foreground_role`), with `task_policy_set` and
`TASK_CATEGORY_POLICY`, which a task may do for itself without privileges,
and logs the role read back with `getpriority(PRIO_DARWIN_ROLE)`
(`darwin role read back: 1`), or a warning if setting it fails. The shorter
`setpriority(PRIO_DARWIN_ROLE, ...)` sets the same role from a terminal but
is refused inside a launchd job (`ENOTSUP`), so it is not used. The evidence
is a probe run as a LaunchAgent, where `setpriority` raised `ENOTSUP` and
`task_policy_set` returned 0 with the role reading back 1; the unit tests run
from a terminal and cannot show the difference. From a
terminal the call makes no difference (microbenchmark 16.6 against 17.1 ms).
With the shipped code in a LaunchAgent, the microbenchmark read role 1 and
took 21 to 26 ms, against 38 ms without it and 14 to 21 ms from a terminal.

`scripts/bench.py --alignments 32 --trials 10` against LaunchAgents, before
and after the fix, on the same machine in one session; each run started only
once no other benchmark or model test was running. The terminal column is the
table above; terminal runs in the same session matched it within noise:

| Metric | Before (LaunchAgent) | After (LaunchAgent) | Terminal |
|---|---|---|---|
| Kokoro first audio | median 87 / worst 89 ms | median 87 / worst 88 ms | median 96 / worst 112 ms |
| Streaming finalize, 64 ms chunks, 200 ms trailing silence | median 3 / p95 31 / worst 37 ms | median 2 / p95 6 / worst 14 ms | median 1 / worst 3 ms |
| Streaming finalize, 64 ms chunks, 0 ms trailing silence | median 145 / p95 168 / worst 192 ms | median 122 / p95 134 / worst 136 ms | median 114 / worst 141 ms |
| Streaming finalize, 100 ms chunks, 200 ms trailing silence | median 3 / p95 7 / worst 45 ms | median 3 / p95 7 / worst 22 ms | median 1 / worst 16 ms |
| Streaming finalize, 100 ms chunks, 0 ms trailing silence | median 147 / p95 161 / worst 168 ms | median 124 / p95 132 / worst 139 ms | median 114 / worst 130 ms |
| Streaming finalize, 64 ms chunks, 200 ms trailing silence, Kokoro synthesizing | median 15 / p95 90 / worst 121 ms | median 2 / p95 75 / worst 87 ms | median 1 / worst 54 ms |
| Streaming finalize, 64 ms chunks, 0 ms trailing silence, Kokoro synthesizing | median 196 / p95 244 / worst 245 ms | median 180 / p95 216 / worst 223 ms | median 140 / worst 192 ms |
| Batch transcription, 10.1 s clip | median 250 / worst 261 ms | median 244 / worst 251 ms | median 253 / worst 258 ms |

Push-to-talk finalize alone went from 31 ms slower than a terminal to 8 ms.
Across three alternating rounds of the shorter run (n=16 per row), the
contention rows were less clear. With 200 ms of trailing silence, Kokoro
synthesizing, the fix helped: median 2 to 17 ms and worst 45 to 102 ms,
against 17 to 29 and 86 to 132 before (terminal 1, and 44 to 60). With 0 ms
there was no clear change: median 176 to 205 ms and worst 213 to 231 after,
against 196 to 204 and 236 to 274 before (terminal 136 to 145). Memory is
unchanged.

**What remains.** In the bench table a terminal is still 8 ms faster alone
(122 against 114 ms median, 64 ms chunks) and 40 ms faster on the 0 ms
contention row (180 against 140 ms), which none of the settings tried above
changed; the untested candidates are listed above. These were
LaunchAgents in the user's GUI domain on macOS 15.7.4. On a Mac Studio
(M1 Max, macOS 27), with the installed LaunchDaemons in the system domain,
the role is set and read back as 1 at start, and the rows above were
re-measured as an A/B ([With the foreground task role](#with-the-foreground-task-role)).
The one row still over target there is 0 ms trailing silence while Kokoro
synthesizes, median 156 to 167 / worst 201 to 222 ms: the GPU contention in
[Known misses](#known-misses), which the role narrows but does not close;
that needs GPU scheduling between the two services, which ADR 0019 adds
and which closes it on the Studio ([With the GPU gate](#with-the-gpu-gate)).
Not yet checked: that
the role still holds after a long idle, with a one-off
`python3 -c "import os; print(os.getpriority(6, PID))"` for each service's
PID (prints 1 while the role holds; it reads another process of the same
user without root).

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
  (operations.md, memory). On the Mac Studio, with an 8B model resident, the
  idle footprint is 2.36 GB.
