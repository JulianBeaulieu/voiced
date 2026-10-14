# 0004: STT engine: Parakeet TDT 0.6B v3 through parakeet-mlx

Date: 2026-10-14. Status: accepted.

## Context

The STT engine behind the frozen `STTEngine` interface is Parakeet TDT 0.6B
v3 (`mlx-community/parakeet-tdt-0.6b-v3`, 25 European languages) via
`parakeet-mlx` 0.5. The interface requires that `add_audio` returns within
tens of milliseconds, that `partial()` never blocks, and that `finalize()`
takes single-digit milliseconds after the last `add_audio`. Endpointing lives
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
attention with 256 frames (20.5 s) of context on each side. Three
measurements shaped the session design:

1. **Every `add_audio` call re-encodes and re-decodes the whole retained
   window.** With a 20.5 s right context nothing is finalized for the first
   20 s, so each call is a full decode of the utterance so far: 150 to 300 ms
   per call, almost independent of how much audio the call adds. Feeding
   100 ms chunks one by one runs at 2.2x real time; 320 ms or more per call
   keeps up (0.6x).
2. **The log-mel is normalized per call**, so calls with little audio have
   poor feature statistics. On an earlier fixture set (macOS `say` output)
   call sizes from 200 to 300 ms and from 640 to 760 ms produced word errors
   ("thermos down" for "thermostat") while 320 to 620 ms matched the batch
   transcript exactly. On the current Kokoro fixtures every size from 200 to
   820 ms matches. The bounds below come from the stricter set.
3. **Calls with fewer than 257 samples (`n_fft // 2 + 1`) read past the
   buffer.** The library's reflect padding assumes at least that many
   samples; a 234-sample tail produced NaN features and a transcript of
   several hundred `<unk>` tokens, nondeterministically. This should be
   reported upstream; the engine never makes such a call.

Context size, 400 ms calls, similarity to the batch transcript (1.00 is
identical, the 0.92 and 0.94 are "twenty" versus "20"):

| context | 3.4 s | 2.8 s | 9.4 s | ms per call |
|---|---|---|---|---|
| (256, 256) | 0.92 | 1.00 | 0.94 | 165 to 205 |
| (256, 128) | 0.92 | 1.00 | 0.94 | 153 to 203 |
| (256, 64) | 0.92 | 1.00 | 0.92 | 154 to 235 |
| (128, 64) | 0.92 | 1.00 | 0.90 | 291 to 315 |

On the `say` fixtures the three smaller contexts had many word errors, since
their finalized region is decoded through an inexact cache (`depth=1`), and
raising `depth` multiplies the retained window and the cost. Smaller
contexts are not faster, so `(256, 256)` with `depth=1` stays and is a
module constant, not an option.

## Decision

`_ChunkedStream` is a synchronous adapter that owns the library's stream
object, the audio buffer, pass sizing, zero padding and the silence flush.
It is the only code that knows the three facts above. `parakeet-mlx` is
pinned to `>=0.5,<0.6`; re-run the sweeps when it changes.

- An encoder pass runs once **320 ms** of audio containing speech is
  buffered; a backlog is split into even passes of at most **640 ms**
  (`MAX_PASS_S >= 2 * MIN_PASS_S` keeps every pass inside the bounds).
- A chunk whose last 96 ms are silent flushes the buffered speech at once,
  zero-padded to 320 ms. This is what makes `finalize()` fast: the tail is
  encoded during the endpointer's silence window, not after it.
- Silence is never encoded. Input is silent when no 32 ms frame exceeds
  -60 dBFS RMS (`SILENCE_FLOOR_DBFS`): the model hallucinates short words
  ("Yeah.") on near-silent noise. Gating on the loudest frame rather than the
  whole-input RMS means long silence cannot average quiet speech away. The
  batch path applies the same gate.
- Streaming timestamps are only emitted while the utterance fits in the left
  context (20.5 s); beyond that the library's token times are relative to the
  retained window and are dropped rather than reported wrong.

`ParakeetStreamSession` buffers in `add_audio` and feeds the adapter from one
consumer task in a worker thread. `finalize()` drains, flushes (skipping a
pending tail that is all silence) and returns the transcript; the library's
stream context, which switches the model's attention globally, is exited in
`close()`, when the server releases the inference slot. The adapter's lock
makes `close()` wait for a pass that is still running, and every blocking
call goes through a `run_blocking` that lets cancellation finish the thread,
so a cancelled `finalize()` or `close()` always leaves the model in full
attention. The engine refuses a second session or a batch call while a
session is open. A failed encoder pass is raised from the next `add_audio`
or `finalize`, never dropped.

Batch `transcribe()` builds the log-mel from the numpy input directly
(`get_logmel` + `generate`): no temp files, no ffmpeg.

### Measured

| What | Value |
|---|---|
| Load from local cache | 0.6 s (6.7 s on a cold disk cache) |
| Warm-up (1 s of silence through batch and streaming paths) | 0.5 s (1.5 s first time) |
| Batch, 3.4 s clip, warm | 102 ms (137 ms for the first call in a process) |
| Batch, 9.4 s clip, warm | 187 ms |
| Streaming, 100 ms chunks paced in real time: max `add_audio` | 0.12 ms |
| Streaming: encoder pass | 150 to 200 ms (3 to 10 s utterance) |
| `finalize()` after a real-time clip plus a 200 ms silence window | 0.6 ms |
| `finalize()` after the fixture with 1 s of trailing silence | 0.2 ms |
| `finalize()` for a 3.4 s clip delivered in one burst | 987 ms (6 passes) |
| Peak resident memory, test process (`ru_maxrss`, includes a 13 s clip) | 1.85 GB |

`finalize()` meets the interface's single-digit-millisecond target because
the flush ran during the silence window. A client that delivers a whole clip
faster than real time pays one encoder pass per 640 ms of audio; the batch
endpoint is the right tool for files.

## VAD choice

`pysilero-vad` 3.4 (MIT) ships the Silero VAD v6 weights in a ggml build: no
torch, no onnxruntime, about 1 MB, prebuilt wheels for macOS arm64 and Linux,
0.15 ms per 32 ms frame. It is a core dependency: endpointing runs outside
the engine and is needed with any engine, and CI exercises it.

`Endpointer` is detector-agnostic: speech starts after 96 ms of speech
frames, ends after `settings.stt.vad_silence_ms` of non-speech frames, both
rounded to the nearest 32 ms frame (200 ms becomes 192 ms). `feed` returns
every event in a chunk so a pause and a new utterance inside one chunk are
both seen. Silero rejects white noise and tones, which is why the synthetic
endpointing tests use `voiced.testing.EnergyDetector`.

Semantic end-of-turn models (smart-turn and the like) are not implemented.
They would slot in as a detector consulted when the silence window elapses,
asking "is this utterance complete?" before `speech_end` is raised, with a
longer maximum wait; that is a change inside `vad.py` plus a config key, not
a server change.

## Consequences

- Streaming accuracy depends on pass size through the library's per-call mel
  normalization. The bounds are measured, not derived.
- Partial hypotheses trail the speaker by one pass (320 to 640 ms of audio
  plus 150 to 200 ms of encoding).
- The silence flush inserts up to 320 ms of zero padding into the stream
  when the speaker pauses and then continues, which shifts later streaming
  timestamps by that much. Text is unaffected.
- The model must not serve a batch request while a stream session is open;
  the engine enforces it and the server's per-engine `InferenceQueue` makes
  it impossible in normal operation.
