# 0004: STT engine: Parakeet TDT 0.6B v3 through parakeet-mlx

Date: 2026-10-14. Status: accepted.

## Context

The STT engine behind the frozen `STTEngine` interface is Parakeet TDT 0.6B
v3 (`mlx-community/parakeet-tdt-0.6b-v3`, CC-BY-4.0, 25 European languages)
via `parakeet-mlx` 0.5. The interface promises that `add_audio` returns within
tens of milliseconds for 100 ms chunks, that `partial()` never blocks, and
that `finalize()` is fast after the last chunk. Endpointing has to live
outside the engine and needs a slot for a semantic end-of-turn model later.

All numbers below were measured on an M1 Max MacBook Pro (64 GB) with the two
fixtures in `tests/fixtures/` (3.4 s and 2.85 s of synthesized English) and
their concatenation (9.6 s). Reproduce with
`VOICED_MODEL_TESTS=1 uv run pytest tests/stt -m model -s`.

## Streaming: what parakeet-mlx actually does

`transcribe_stream(context_size=(256, 256))` keeps the encoder in local
attention with 256 frames (20.5 s) of context on each side. Three
measurements shaped the session design:

1. **Every `add_audio` call re-encodes and re-decodes the whole retained
   window.** With a 20.5 s right context nothing is "finalized" for the first
   20 s, so each call is a full decode of the utterance so far: 150 to 300 ms
   per call on this machine, almost independent of how much audio the call
   adds. Feeding 100 ms chunks one by one therefore runs at 2.2x real time;
   320 ms or more per call keeps up (0.6x).
2. **The log-mel is normalized per call.** Calls with little audio have poor
   feature statistics and produce worse text. Fixed call sizes from 200 to
   820 ms were swept on all three inputs: everything from 320 to 620 ms
   matched the batch transcript exactly; 200 to 300 ms and 640 to 760 ms had
   word errors ("thermos down" for "thermostat").
3. **Calls with only a few hundred samples read past the buffer.** The
   library's reflect padding assumes at least 257 samples; a 234-sample tail
   produced NaN features and a transcript of several hundred `<unk>` tokens,
   nondeterministically.

Smaller contexts were tried and rejected: `(256, 128)`, `(256, 64)` and
`(128, 64)` are not meaningfully faster (the per-call overhead dominates) and
are much less accurate because the finalized region is decoded through an
inexact cache (`depth=1`). Raising `depth` multiplies the retained window and
the cost. `(256, 256)` with `depth=1` stays, and both are engine constructor
options for experiments.

## Decision

`ParakeetStreamSession` never calls the encoder from `add_audio`. It appends
to a buffer and returns (measured maximum 0.26 ms). A single background task
runs encoder passes in a worker thread whenever at least **320 ms** of audio
is buffered, splitting any backlog into even passes of at most **600 ms**.
`partial()` returns the hypothesis from the last pass. `finalize()` sets a
flush flag, waits for the loop to drain the buffer (a final tail shorter than
320 ms is zero-padded to 320 ms so the library never sees a short call), then
exits the stream context so the model is back in full attention before the
server releases the inference slot.

Batch `transcribe()` builds the log-mel from the numpy input directly
(`get_logmel` + `generate`), so no temp files and no ffmpeg; the API layer
decodes uploads. Segments come from the library's sentence alignment.

Input quieter than -60 dBFS RMS is returned as an empty transcript without
touching the model, in both paths: the model hallucinates short words
("Yeah.") on near-silent noise.

Streaming timestamps are only emitted while the utterance fits in the left
context (20.5 s); beyond that the library's token times are relative to the
retained window and are dropped rather than reported wrong.

### Measured

| What | Value |
|---|---|
| Load from local cache | 0.6 s (6.7 s on a cold disk cache) |
| Warm-up (1 s of silence through batch and streaming paths) | 0.6 s (1.5 s first time) |
| Batch, 3.4 s clip, warm | 68 ms (135 ms for the first call in a process) |
| Streaming, 100 ms chunks paced in real time: max `add_audio` | 0.26 ms |
| Streaming: backlog when the speaker stops | 181 ms |
| Streaming: encoder pass | 150 to 200 ms (3 to 10 s utterance) |
| `finalize()` after the last chunk, 200 ms silence window elapsed | 134 ms (one pass for the tail) |
| `finalize()` overhead beyond the encoder pass | 33 ms |
| `finalize()` for 3.4 s delivered in one burst | 1.06 s (6 passes) |
| Peak resident memory with the model loaded, test process | 1.22 GB |

The brief's target was "finalize within 50 ms of the last chunk (reference
measured 3 ms)". The reference number is `result()` after a synchronous
`add_audio` of the silence chunk, whose 150 ms encode was hidden inside the
200 ms VAD sleep. The comparable number here is the 33 ms of overhead beyond
the final encoder pass (thread hop, sentence assembly, restoring the attention
mode). The number a client sees is one encoder pass, about 130 to 200 ms,
because the trailing audio that arrived during the silence window still has
to be encoded. Skipping that tail when it is silent would get close to the
reference number but could drop the last word; correctness wins for v1.

## VAD choice

`pysilero-vad` 3.4 (MIT, by the Rhasspy and Home Assistant author) ships the
Silero VAD v6 weights in a ggml build: no torch, no onnxruntime, about 1 MB,
prebuilt wheels for macOS arm64 and Linux, 0.15 ms per 32 ms frame. It is a
core dependency so CI exercises it, with `EnergyDetector` as the fallback if
the import fails. Silero rejects white noise and synthetic signals, so the
synthetic endpointing tests use the energy detector and the Silero tests use
the real speech fixture.

`SilenceEndpointer` is detector-agnostic: speech starts after 96 ms of speech
frames, ends after `settings.stt.vad_silence_ms` (default 200 ms, rounded up
to whole 32 ms frames) of non-speech frames. The semantic end-of-turn hook is
the `TurnDetector` protocol: when the silence window elapses the endpointer
asks `is_turn_complete(utterance_audio)` and otherwise waits, re-asking every
window up to `max_silence_ms`. smart-turn v3 would be a `TurnDetector`
implementation plus a config key; nothing in the server changes.

## Consequences

- Streaming accuracy depends on pass size through the library's per-call
  mel normalization. The 320 to 600 ms bounds are a measurement, not a
  derivation; re-sweep them when `parakeet-mlx` changes its streaming code.
  A proper fix (normalizing the mel over the whole utterance) would mean
  reimplementing the library's `add_audio` and is deferred.
- Partial hypotheses update every 320 to 600 ms of audio plus one encoder
  pass, so about half a second behind the speaker.
- A client that sends a whole clip at once through the streaming API gets
  batch-quality text but pays one encoder pass per 600 ms of audio; the
  batch endpoint is the right tool for files.
- The model must not serve a batch request while a stream session is open
  (attention mode is global state). The server's per-engine `InferenceQueue`
  already guarantees that.
- `pysilero-vad` becomes a core dependency, which the brief did not ask for
  explicitly; the alternative was an `stt`-only VAD that CI could not test.
