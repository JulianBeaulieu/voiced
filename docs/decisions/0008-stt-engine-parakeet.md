# 0008: STT engine: Parakeet TDT 0.6B v3 through parakeet-mlx

Date: 2026-10-14. Status: accepted.

## Context

The STT engine behind the frozen `STTEngine` interface is Parakeet TDT 0.6B
v3 (`mlx-community/parakeet-tdt-0.6b-v3`, 25 European languages) via
`parakeet-mlx` 0.5. The interface requires that `add_audio` returns within
tens of milliseconds, that `partial()` never blocks, and that `finalize()`
takes single-digit milliseconds after the last `add_audio`; the acceptance
target is finalize within 50 ms of the end of speech. Endpointing lives
outside the engine.

The weights are CC-BY-4.0: voiced downloads them from Hugging Face on first
run and does not ship them; anyone redistributing them owes NVIDIA
attribution under that license.

All numbers below were measured on an M1 Max (64 GB) with the fixtures in
`tests/fixtures/` (3.4 s and 2.8 s of English synthesized with Kokoro) and
their concatenation (9.4 s). Reproduce with
`VOICED_MODEL_TESTS=1 uv run pytest tests/stt -m model -s`.

## What parakeet-mlx streaming actually does

`transcribe_stream(context_size=(256, 256))` keeps the encoder in local
attention with 256 frames (20.5 s) of context on each side. Four
measurements shaped the design:

1. **Every `add_audio` call re-encodes and re-decodes the whole retained
   window.** With a 20.5 s right context nothing is finalized for the first
   20 s, so each call is a full decode of the utterance so far: 150 to 300 ms
   per call, almost independent of how much audio the call adds. Feeding
   100 ms chunks one by one runs at 2.2x real time.
2. **The log-mel is normalized per call.** Call sizes from 320 to 620 ms
   matched the batch transcript on every fixture tried; 200 to 300 ms and
   640 to 760 ms produced word errors on earlier test audio. Because of the
   normalization, the transcript depends on where the call boundaries fall.
3. **Short calls corrupt the stream.** Calls under 257 samples
   (`n_fft // 2 + 1`) read past the buffer in the reflect padding and give
   NaN features. Calls of a few dozen milliseconds, above that limit, made
   the hypothesis collapse to an empty string mid-utterance and once left the
   final transcript empty. The 257-sample read is a library bug worth
   reporting upstream; it has not been filed from here.
4. **A call of room noise alone is decoded as speech.** Normalized per call,
   -55 dBFS noise looks like a loud utterance. A noise-only first call added
   invented words ("We have a turn on the kitchen lights", "Either you could
   turn on ...").

Context size, 400 ms calls, word similarity to the batch transcript (1.00 is
identical; 0.92 and 0.94 are "twenty" against "20"):

| context | 3.4 s | 2.8 s | 9.4 s | ms per call |
|---|---|---|---|---|
| (256, 256) | 0.92 | 1.00 | 0.94 | 165 to 205 |
| (256, 128) | 0.92 | 1.00 | 0.94 | 153 to 203 |
| (256, 64) | 0.92 | 1.00 | 0.92 | 154 to 235 |
| (128, 64) | 0.92 | 1.00 | 0.90 | 291 to 315 |

Smaller contexts are not faster and were less accurate on earlier audio
(their finalized region is decoded through an inexact cache with
`depth=1`), so `(256, 256)` with `depth=1` stays, as module constants.

## Decision

`_ChunkedStream` is a synchronous adapter that owns the library's stream
object, the audio buffer and every rule below; it is the only code that
knows the four facts above. `parakeet-mlx` is pinned to `>=0.5,<0.6`;
re-run the measurements when it changes. The session in front of it only
buffers in `add_audio` (0.1 ms) and feeds the adapter from one consumer task
through `voiced.threads.run_blocking`.

- **Speech-start gate.** Nothing is encoded until a Silero `Endpointer`
  (`voiced.vad`, the same detector and `min_speech_ms` the transports use)
  reports speech start; then the adapter starts from `PRE_ROLL_S` (300 ms)
  before the onset. This removes fact 4. A level gate cannot do it: before
  the speaker starts there is no speech level to be relative to, and room
  noise sits above any fixed floor that still passes quiet speech.
- **Fixed pass grid.** Every regular encoder call is exactly `PASS_S`
  (400 ms, the middle of the safe band in fact 2) at fixed offsets from the
  start of speech, so the transcript is a function of the audio alone, not of
  how it was chunked or how busy the GPU was (tested for chunk sizes from
  10 ms to 1 s). No call is ever shorter than `PASS_S` (fact 3); a short
  final window is zero-padded to it.
- **End-of-speech flush.** A chunk whose last 32 ms fall 35 dB below the
  loudest frame of the session (floored at -60 dBFS) is a pause. Relative, so
  a real microphone noise floor neither re-arms the buffer nor blocks the
  flush. On a pause with less than a pass buffered, everything buffered is
  encoded at once, padded to `PASS_S`, so by the time the endpointer's
  silence window (192 ms) has elapsed `finalize()` usually has nothing left
  to do.
- **The flush is speculative.** Before that call the adapter snapshots the
  library's stream state (encoder caches, mel and audio buffers, decoder
  state, token lists; MLX copies are lazy, about 2 ms for every layer). If
  speech resumes, the snapshot is restored and the held audio replayed on the
  grid, so the zero padding never becomes part of the transcript and the
  state is exactly what it would have been with no flush. This reaches into
  `StreamingParakeet`'s fields, which is why it sits in the adapter behind the
  version pin; the fake model in the tests carries the same fields.
- **Silence is never encoded on its own.** The batch path returns an empty
  transcript when no 32 ms frame reaches -60 dBFS (the model says "Yeah." on
  near-silent noise); gating on the loudest frame means long silence cannot
  average quiet speech away.
- **Bounds.** `add_audio` rejects audio beyond a 30 s backlog (a client
  feeding faster than real time while holding the only slot). Streaming
  timestamps are kept only while the utterance fits in the left context
  (20.5 s); past that the library's token times are relative to the retained
  window and are dropped rather than reported wrong.

The engine refuses a second session or a batch call while a session is open
(attention mode is global), and restores full attention in `close()`, which
waits for a running call even when cancelled. A failed encoder call is
raised from the next `add_audio` or `finalize`.

Batch `transcribe()` builds the log-mel from the numpy input directly
(`get_logmel` + `generate`): no temp files, no ffmpeg.

## Measured

Finalize latency, from the end of the endpointer's 192 ms silence window to
`finalize()` returning, over 32 end-of-speech alignments (every 20 ms of
lead-in from 0 to 620 ms) per row, real-time paced:

| Chunks | After the speech | Worst | p95 | Median | Over 50 ms |
|---|---|---|---|---|---|
| 100 ms | digital silence | 120 ms | 114 ms | 10 ms | 15 of 32 |
| 100 ms | -55 dBFS room noise, speech at -6 dBFS | 109 ms | 107 ms | 2 ms | 14 of 32 |
| 64 ms | digital silence | 163 ms | 162 ms | 54 ms | 17 of 32 |
| 64 ms | -55 dBFS room noise, speech at -6 dBFS | 156 ms | 155 ms | 21 ms | 15 of 32 |

The distribution is bimodal: a few milliseconds, or about one encoder call.
For comparison, the simple rule (no flush before `finalize()`), over 16
alignments with digital silence:

| Chunks | Worst | Median | Over 50 ms |
|---|---|---|---|
| 100 ms | 223 ms | 97 ms | 8 of 16 |
| 64 ms | 215 ms | 176 ms | 11 of 16 |

Other numbers:

| What | Value |
|---|---|
| Load from local cache plus warm-up | 1.2 s (about 8 s with a cold disk cache) |
| Batch, 3.4 s clip, first call in the process | 130 ms |
| Batch, 9.4 s clip, warm | 163 ms |
| `add_audio`, max | 0.35 ms |
| `finalize()` after the fixture with 1 s of trailing silence | 1.9 ms |
| `finalize()` for a 3.4 s clip delivered in one burst | 1.46 s (one call per 400 ms) |
| Pauses of 110, 250 and 400 ms between words, flush on against off, real weights | identical text, 6 to 9 rollbacks each |
| Room noise before speech (-55 dBFS, 300 to 800 ms lead-in, 30 runs) | 0 of 30 with invented leading words, gate off and gate on |
| Peak resident memory, model test process | 1.89 GB |

The invented leading words of fact 4 appeared in 2 of about 50 runs on
earlier versions of the pass logic and in none of 30 on the final grid, with
or without the gate. The gate stays because it also keeps a session of pure
room noise from being encoded at all, and because nothing before speech
start is worth the encoder's time.

With pauses of 250 ms inserted between every word, streaming reads "kitchen
light" or "connection light" where batch reads "kitchen lights", with the
flush on or off alike: streaming accuracy depends on where the grid calls
fall relative to long pauses.

### Why the worst case misses 50 ms

An encoder call takes about 195 ms for a 3 s utterance, and the endpointer
window is 192 ms. When a regular grid call happens to start in the last
200 ms of speech, the end-of-speech flush can only start once it finishes:
two calls after the speaker stops, about 390 ms, so `finalize()` waits 100 to
165 ms. Traced on the worst alignments: a grid call from -49 ms to +145 ms
around the end of speech, then the flush from +146 ms to +340 ms, finalize
called at +207 ms. Every design that encodes during speech has this case;
the speculative flush only removes the call that would otherwise always
follow `finalize()`. The alternative rule, flush only at `finalize()`, is
shown in the table for comparison: it pays a full call on every utterance.

Ways to close the gap, none taken: a cheaper call (the library re-decodes the
whole window each time), a longer endpointer window, or larger grid passes
(fewer boundaries to collide with, at the cost of later partials). The 50 ms
target is the owner's to keep or relax with these numbers.

## VAD

`pysilero-vad` 3.4 (MIT, by the author of the Rhasspy and Wyoming projects)
ships the Silero VAD v6.2 weights (MIT) converted to ggml, 885 KB, inside the
wheel, with ggml's CPU libraries (MIT): no torch, no onnxruntime, no Python
dependencies, nothing downloaded at runtime, about 4.8 MB installed, prebuilt
wheels for macOS arm64 and Linux, 0.15 ms per 32 ms frame. It is a core
dependency because both the engine (speech start) and the transports
(speech end) use it; that is also why the module is `voiced.vad` at the top
level, per ADR 0001.

`Endpointer` is detector-agnostic: speech starts after 96 ms of speech
frames and ends after `settings.stt.vad_silence_ms` of non-speech frames,
both rounded to the nearest 32 ms frame (200 ms becomes 192 ms). `feed`
returns every event in a chunk, so a pause and a new utterance inside one
chunk are both seen. Silero rejects white noise and tones, which is why the
synthetic endpointing tests use `voiced.testing.EnergyDetector`.

Semantic end-of-turn models (smart-turn and the like) are not implemented.
They would be a detector consulted when the silence window elapses, asking
whether the utterance is complete before `speech_end` is raised, with a
longer maximum wait: a change inside `voiced.vad` plus a config key.

## Consequences

- Partial hypotheses trail the speaker by up to one pass plus one call
  (400 ms of audio plus about 200 ms).
- Each pause inside an utterance costs one discarded encoder call. At about
  1.5 pauses per utterance the GPU stays well under real time, but a pause
  close to the end of speech adds to the worst case above.
- A client that delivers a whole clip at once through the streaming API pays
  one call per 400 ms of audio; the batch endpoint is the right tool for
  files.
- The model must not serve a batch request while a stream session is open;
  the engine enforces it and the server's per-engine `InferenceQueue` makes
  it impossible in normal operation.
