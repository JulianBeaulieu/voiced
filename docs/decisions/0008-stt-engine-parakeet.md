# 0008: STT engine: Parakeet TDT 0.6B v3 through parakeet-mlx

Date: 2026-10-14. Status: accepted.

## Context

The STT engine behind the frozen `STTEngine` interface is Parakeet TDT 0.6B
v3 (`mlx-community/parakeet-tdt-0.6b-v3`, 25 European languages) via
`parakeet-mlx` 0.5. The interface requires that `add_audio` returns within
tens of milliseconds, that `partial()` never blocks, and that `finalize()`
is fast after the last `add_audio`. The acceptance target (DECISIONS.md,
2026-10-14) is a finalize within 50 ms of the end of the endpointer's silence
window if a simple design reaches it, otherwise a worst case of 150 ms across
chunk sizes, alignments and a -55 dBFS noise floor. End of speech is decided
outside the engine.

The weights are CC-BY-4.0: voiced downloads them from Hugging Face on first
run and does not ship them; anyone redistributing them owes NVIDIA
attribution under that license.

All numbers below were measured on an M1 Max (64 GB) with the fixtures in
`tests/fixtures/` (3.4 s and 2.8 s of English synthesized with Kokoro) and a
continuous 27 s passage synthesized the same way and cut at pauses into 5,
10, 15 and 27 s utterances. Reproduce the fixture numbers with
`VOICED_MODEL_TESTS=1 uv run pytest tests/stt -m model -s`.

## What parakeet-mlx streaming actually does

`transcribe_stream(context_size=(left, right))` switches the encoder to local
attention. Six facts shaped the design:

1. **Every call re-encodes and re-decodes the right context.** The library
   keeps the last `right` encoder frames (80 ms each) of log-mel as an
   unfinalized draft and runs the encoder and the TDT decoder over all of it,
   plus the new audio, on every call; tokens are final only once they fall out
   of it. With the library's example `(256, 256)` that draft is up to 20.5 s,
   so a call costs more the longer the utterance: 174 ms at 3.4 s, 262 ms at
   10 s, 317 ms at 15 s, 399 to 426 ms at 25 s (a 400 ms call falls behind real
   time). About half of it is the decoder, a Python loop over the draft frames.
2. **The log-mel is normalized per call,** so the transcript depends on where
   call boundaries fall, and a call shorter than the right context's lookahead
   needs loses words: with a 32-frame right context and 400 ms calls the start
   of every utterance came out garbled ("Good, please turn on the hitch andpen
   the lines").
3. **Short calls corrupt the stream.** Calls under 257 samples
   (`n_fft // 2 + 1`) read past the buffer in the reflect padding and give NaN
   features (a library bug, not yet filed upstream); calls of a few dozen
   milliseconds above that collapsed the hypothesis mid-utterance.
4. **A call of room noise alone is decoded as speech.** Normalized per call,
   -55 dBFS noise looks like a loud utterance; a noise-only first call added
   invented words ("We have a turn on the kitchen lights").
5. **Token times are relative to the retained mel buffer,** which is trimmed
   to the right context, and each call of `n` samples becomes `n // 160 + 1` mel
   frames (a 400 ms call is 410 ms of model time). Library timestamps drift and
   stop being absolute once an utterance outgrows the right context.
6. **MLX streams are per thread.** Entering the stream context and loading
   the weights create arrays on the calling thread's GPU stream; a later call
   on another thread fails with "There is no Stream(gpu, 0) in current
   thread". Batch calls on the loaded model and Kokoro synthesis did not fail
   across threads (probed: 0 of 6 each); a stream did, 4 of 4.

### Context size

Word similarity to the batch transcript of the same audio (1.00 is
identical; 0.92 on the 3.4 s fixture is "twenty" against "20"), and the
slowest of the last calls, by call size:

| Context, call | 3.4 s | 5 s | 10 s | 15 s | 27 s | Call cost |
|---|---|---|---|---|---|---|
| (256, 256), 400 ms | 0.92 | 1.00 | 1.00 | 1.00 | 0.99 | 191 to 426 ms, grows |
| (256, 32), 400 ms | 0.92 | 0.69 | 0.75 | 0.76 | 0.76 | 171 to 195 ms |
| (128, 32), 400 ms | 0.92 | 0.69 | 0.75 | 0.76 | 0.76 | 115 to 125 ms |
| (256, 16), 400 ms | 0.92 | 0.94 | 0.89 | 0.81 | 0.76 | 153 to 173 ms |
| (128, 128), 400 ms | 0.92 | 1.00 | 1.00 | 1.00 | 0.93 | 99 to 219 ms |
| (128, 64), 800 ms | 0.92 | 1.00 | 0.97 | 0.99 | 0.90 | 112 to 160 ms |
| (64, 32), 800 ms | 0.92 | 0.00 | 0.77 | 0.77 | 0.88 | 90 to 108 ms |
| (128, 32), 1200 ms | 1.00 | 1.00 | 0.99 | 0.89 | 0.95 | 115 to 134 ms |
| **(64, 64), 800 ms** | 0.92 | 1.00 | 1.00 | 0.99 | 0.98 | 96 to 144 ms |
| (64, 64), 1200 ms | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 96 to 145 ms |

`(64, 64)` with 800 ms calls is the smallest context whose accuracy holds;
1200 ms calls are as accurate but make partials trail by another 400 ms.
The left context matters little for accuracy; the right context and the call
size do.

## Decision

`CONTEXT_SIZE = (64, 64)` and `PASS_S = 0.8`: a call costs 100 to 200 ms at
any utterance length, shorter than the endpointer's 192 ms silence window.
`parakeet-mlx` is pinned to `>=0.5,<0.6`; re-run the measurements when it
changes.

Streaming is three pieces in `voiced.engines.parakeet`:

- **`_Planner`** (numpy only) decides what to encode and when, from the audio
  and a Silero `Endpointer` (`voiced.vad`, the detector the transports will use).
  - *Speech-start gate.* Nothing is encoded until the detector reports speech
    start, then from `PRE_ROLL_S` (300 ms) before it (fact 4). A level gate
    cannot do this: before the speaker starts there is no level to be relative
    to.
  - *Fixed grid.* Every regular call is exactly `PASS_S` at fixed offsets from
    the start of speech, so the transcript is a function of the audio, not of
    how it was chunked or how busy the GPU was (facts 2 and 3). A pass in
    which no 32 ms frame is speech is never encoded (the model invents words
    in a call of silence): it is dropped and the grid restarts `PRE_ROLL_S`
    before the next speech frame, the same lead-in as at speech start. A long
    pause keeps at most one pass of audio meanwhile.
  - *Speculative final call.* When the last 32 ms frame is not speech (Silero's
    per-frame decision, `Endpointer.last_speech_s`) and less than a pass is
    buffered, the planner checkpoints the stream and encodes the buffer at
    once, zero-padded to a pass. The buffer is kept: if speech resumes, or the
    pause (measured from the last speech frame) outgrows `HOLD_MAX_S` (2.5 s),
    the checkpoint is restored and the grid goes on as if there had been no
    pause, so the padding never becomes
    part of the transcript. If not, that call is the final one and
    `finalize()` has nothing left to encode.
- **`_LibraryStream`** is the only code that touches `StreamingParakeet`:
  `encode(window, start)`, `checkpoint()`, `restore()`, `result()`, `close()`.
  It records the session position of every window and maps each token's time
  back through it (fact 5), so segments are in session time after the gate,
  across dropped pauses and at any length. The checkpoint copies 7 stream
  fields and the 4 per encoder layer (MLX copies are lazy).
- **`ParakeetStreamSession`** buffers in `add_audio` and feeds both from one
  consumer task. `add_audio` rejects audio past a 30 s backlog, counting what
  is pending, the chunk in the running call and what the planner holds.

Every MLX call of the engine, `load()` included, runs on the engine's own
`voiced.threads.ModelThread` (fact 6): one thread for the engine's life, with
`run_blocking`'s rule that a cancelled caller waits for the call to finish.

The engine refuses a second session or a batch call while a session is open
(attention mode is global), and restores full attention in `close()`, which
waits for a running call even when cancelled. A failed encoder call is raised
from the next `add_audio` or `finalize`.

Batch `transcribe()` builds the log-mel from the numpy input directly
(`get_logmel` + `generate`, in 20 s chunks past 20 s; see Consequences), and returns an empty transcript when no 32 ms
frame reaches -60 dBFS (the model says "Yeah." on near-silent noise).

### Why the speculative call stays

With the cheaper call, the alternative of encoding the rest only when
`finalize()` is called, measured over the same alignments (the same code
with the planner's pause step removed):

| Case | Speculative: worst, median | At finalize: worst, median |
|---|---|---|
| 3.4 s, digital silence, 64 ms chunks | 2, 1 ms | 130, 120 ms |
| 3.4 s, -55 dBFS room, 100 ms chunks | 3, 1 ms | 133, 123 ms |
| 10.4 s, 100 ms chunks | 3, 1 ms | 167, 151 ms |
| 15.0 s, 100 ms chunks | 1, 1 ms | 170, 79 ms |
| 26.6 s, 100 ms chunks | 3, 1 ms | 166, 148 ms |

Encoding at finalize pays one call on every utterance and misses the 150 ms
worst case past 10 s. The speculative call meets the 50 ms target; it costs
the checkpoint and restore, which sit behind the library seam, and one
discarded call per pause inside an utterance.

## Measured

Finalize latency, from the end of the endpointer's 192 ms silence window to
`finalize()` returning, real-time paced. This meets the 50 ms target in the
median everywhere and the 150 ms worst case everywhere measured.

The 3.4 s fixture, 32 end-of-speech alignments per row (every 20 ms of
lead-in from 0 to 620 ms):

| Chunks | After the speech | Worst | p95 | Median | Over 50 ms |
|---|---|---|---|---|---|
| 64 ms | digital silence | 4 ms | 1 ms | 0 ms | 0 of 32 |
| 100 ms | digital silence | 27 ms | 1 ms | 1 ms | 0 of 32 |
| 64 ms | -55 dBFS room noise, speech at -6 dBFS | 4 ms | 3 ms | 1 ms | 0 of 32 |
| 100 ms | -55 dBFS room noise, speech at -6 dBFS | 35 ms | 31 ms | 1 ms | 0 of 32 |

Speech level against room noise, 64 ms chunks, 8 alignments per cell, worst
and median (the earlier relative energy gate needed about 35 dB between
speech peak and noise and gave 105 to 208 ms medians below it):

| Speech peak | -55 dBFS noise | -50 dBFS noise | -45 dBFS noise |
|---|---|---|---|
| -6 dBFS | 1, 0 ms | 3, 0 ms | 131, 0 ms |
| -20 dBFS | 3, 0 ms | 48, 0 ms | 3, 0 ms |
| -25 dBFS | 59, 1 ms | 56, 1 ms | 1, 0 ms |

Utterance length, digital silence after, 8 alignments per row (every 50 ms
of lead-in), similarity to the batch transcript of the same audio:

| Utterance | Call, median and max | 64 ms chunks: worst, median | 100 ms chunks: worst, median | Similarity |
|---|---|---|---|---|
| 5.3 s | 102, 155 ms | 59, 0 ms | 102, 1 ms | 0.92 |
| 10.4 s | 133, 178 ms | 37, 0 ms | 1, 1 ms | 0.92 |
| 15.0 s | 146, 186 ms | 61, 5 ms | 14, 0 ms | 0.97 |
| 26.6 s | 152, 190 ms | 1, 1 ms | 2, 1 ms | 0.94 |

With `(256, 256)` the same 25 s utterance cost 433 ms per call and a
1.3 s finalize median (measured with `(256, 256)`). The few alignments between 50 and
131 ms are not traced; they stay under one encoder call, and the likely cause
is a regular call that started just before the final one.

Timestamps: with a 1.5 s lead-in, a dropped 1.5 s pause and a 15 s
utterance, streamed token times are within 0.2 s of batch token times for
the same words.

Other numbers:

| What | Value |
|---|---|
| Load from local cache plus warm-up | 1.2 s (about 8 s with a cold disk cache) |
| Batch, 9.4 s clip, warm | 163 ms |
| `finalize()` for a 3.4 s clip delivered in one burst | 466 ms (one call per 800 ms) |
| `add_audio`, max | under 1 ms |
| Pauses of 110, 250 and 400 ms between words | text identical to encoding the kept windows with no checkpoint or restore, real weights |
| Room noise before speech (-55 dBFS, 300 to 800 ms lead-in) | no invented leading words |
| Speculative calls undone | about one per pause inside an utterance (9 per 27 s passage) |

## Constants

| Constant | Value | Load-bearing because |
|---|---|---|
| `CONTEXT_SIZE` | (64, 64) | right context sets call cost (fact 1) and accuracy (fact 2) |
| `PASS_S` | 0.8 s | shorter calls lose words with this context; must stay below the right context (5.1 s) |
| `PRE_ROLL_S` | 0.3 s | onset audio before Silero's 96 ms confirmation |
| `HOLD_MAX_S` | 2.5 s | bounds how long a pause is held before the grid resumes |
| `BATCH_CHUNK_S`, `BATCH_OVERLAP_S` | 20 s, 4 s | bound batch memory (see Consequences) |
| `MAX_BACKLOG_S` | 30 s | bounds memory for a client feeding faster than real time |
| `SILENCE_FLOOR_DBFS` | -60 | batch only: skip near-silent input |
| `LIBRARY_MIN_SAMPLES` | 257 | batch only, defensive (fact 3) |
| `WARMUP_S` | 1.0 s | warm-up input length; not tuned |

## VAD

`pysilero-vad` 3.4 (MIT, by the author of the Rhasspy and Wyoming projects)
ships the Silero VAD v6.2 weights (MIT) converted to ggml, 885 KB, inside the
wheel, with ggml's CPU libraries (MIT): no torch, no onnxruntime, no Python
dependencies, nothing downloaded at runtime, about 4.8 MB installed, prebuilt
wheels for macOS arm64 and Linux, about 0.12 ms per 32 ms frame. It is a core
dependency because the engine uses it and the transports will; that is also
why the module is `voiced.vad` at the top level, per ADR 0001.

`Endpointer` is detector-agnostic: speech starts after 96 ms of speech
frames and ends after `settings.stt.vad_silence_ms` of non-speech frames,
both rounded to the nearest 32 ms frame (200 ms becomes 192 ms). `feed`
returns every event in a chunk; `last_speech_s` exposes the per-frame
decision the engine uses for pauses. The engine and the transports run the
same detector at the same threshold on the same audio, so they agree on what
a pause is; only the transports end the turn. Silero rejects white noise and
tones, which is why the synthetic tests use `voiced.testing.EnergyDetector`.

Semantic end-of-turn models (smart-turn and the like) are not implemented.
They would be a detector consulted when the silence window elapses, asking
whether the utterance is complete before `speech_end` is raised, with a
longer maximum wait: a change inside `voiced.vad` plus a config key.

## Consequences

- Partial hypotheses trail the speaker by up to one pass plus one call. The
  first partial arrives a median 0.7 s after the first word (0.6 to 0.8 s,
  100 ms chunks); a 400 ms grid gave 0.3 s but loses words at this context
  (fact 2). This is the price of the finalize target; nothing recovers it
  without a second grid.
- Each pause inside an utterance costs one discarded call.
- Streaming accuracy depends on where grid calls fall against long pauses.
  With silence inserted at every gap between words, streaming read "kitchen
  light" (250 ms gaps) and "set the the th thermostat" (400 ms gaps) where
  batch is right; encoding the same windows straight through, with no
  checkpoint or restore, gives the same text, so the speculative call is not
  the cause. One pause of 1.0 s between "set the" and "thermostat" gave "the
  the" at 3 of 4 alignments, again with or without the speculative call;
  0.6, 0.8 and 1.2 to 4.0 s pauses were clean at every alignment. Natural
  speech (the fixtures and the 27 s passage) scores 0.92 to 1.00 against
  batch.
- A client that delivers a whole clip at once through the streaming API pays
  one call per 800 ms of audio; the batch endpoint is the right tool for
  files.
- The model must not serve a batch request while a stream session is open;
  the engine enforces it and the server's per-engine `InferenceQueue` makes
  it impossible in normal operation.
- Memory (MLX allocations, `mx.get_peak_memory()`, a process-wide peak):
  1.28 GB held after load. Batch input longer than `BATCH_CHUNK_S` (20 s) is
  transcribed in 20 s chunks overlapping by 4 s and merged on their tokens, as
  parakeet-mlx's own file transcription does, and MLX's buffer cache is
  emptied after each request. Serving then peaks at 2.46 GB for any input
  length (a 4 minute request, and a stream; in one piece a 62 s request
  peaked at 4.1 GB and 5 minutes at 6.4 GB, with as much again held in the
  cache), inside the 3 GB budget for STT and TTS together (Kokoro is 469 MB
  on its own). Chunked text matched the one-piece transcript at 0.985 (62 s)
  to 0.999 (5 min) word accuracy. Loading peaks once at 3.6 to 4.1 GB,
  because parakeet-mlx reads the fp32 weights and then casts them to bf16;
  that transient is over the budget at startup. Loading tensor by tensor
  would avoid it; that is a library change, not made here.
