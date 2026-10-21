# 0032: Whisper large-v3-turbo ships selectable, for its 100 languages

Date: 2026-10-21. Status: accepted.

## Context

Whisper large-v3-turbo is OpenAI's large-v3 with 4 decoder layers instead
of 32: 809M parameters, 100 language codes (large-v3 added Cantonese to the
original 99), the recogniser most OpenAI clients expect (issue #60). On the
Open ASR Leaderboard it averages 8.39 WER at an RTFx of 811, against 6.51
at 6228 for Parakeet TDT 0.6B v3: worse and slower in English. Its reason
to be in voiced is breadth, not speed. It has no streaming: the encoder
always sees a 30 s window, padded.

Checked on an Apple M1 Max (64 GB, macOS 15.7.4) with Python 3.12 before
building anything:

- **Runtime.** mlx-whisper 0.4.3 declares `torch` and `numba` as
  dependencies, so it is out. mlx-audio 0.5.9, already in the `stt` extra,
  has a port of Whisper's own `transcribe` loop (30 s windows, temperature
  fallback, word timestamps by cross-attention and dynamic time warping,
  `hallucination_silence_threshold`). No new package.
- **Repositories.** `mlx-community/whisper-large-v3-turbo` is mlx-whisper's
  layout (one weights file and a short config); mlx-audio needs the
  tokenizer and feature extractor beside the weights, which the
  `mlx-community/whisper-large-v3-turbo-asr-*` conversions have (fp16,
  8, 6, 5 and 4 bit).
- **Cost.** The encoder's 30 s window alone takes about 400 ms in fp16 and
  470 ms in 8 bit, so every call costs about the same whatever the length
  of the audio: 450 to 530 ms with a language hint for 3, 10 or 30 s of
  speech. Without a hint mlx-audio detects the language with a pass of its
  own before decoding, about 0.9 to 1.0 s. Word timestamps add another
  forward pass, also about 0.45 s.
- **Hallucination.** Digital silence, white noise from -55 to -30 dBFS
  and a 50 Hz hum at -30 dBFS all came back as "Thank you." or "You",
  hinted or not. The port's `no_speech_prob` was 0.0 on every input, so
  Whisper's own no-speech check never fires.
- **Memory.** MLX active after load: fp16 1.62 GB, 8 bit 0.87 GB, 4 bit
  0.47 GB; peaks 2.48, 1.73 and 1.33 GB. The three gave the same
  transcripts on the English and Spanish fixtures.
- It caches in the standard Hugging Face hub cache.

## Licences

| What | Licence | Source |
|---|---|---|
| `openai/whisper-large-v3-turbo`, the weights | MIT | model card, `license: mit` |
| `mlx-community/whisper-large-v3-turbo-asr-8bit`, `-asr-fp16` | none stated; converted from the above | model cards (no `license` field) |
| mlx-audio, the runtime | MIT | already a dependency |

The catalogue records MIT, the original's licence, which a conversion
cannot narrow. voiced redistributes no weights.

## Decision

`whisper` is an STT engine, `voiced.engines.whisper.WhisperEngine`,
selectable and never the default. It is the engine for languages Parakeet
does not have, and for the clients that expect Whisper; it is not a
live-assistant engine on this hardware.

- **Dependency.** None new: mlx-audio at the `stt` extra's range. The
  pin's comment in `pyproject.toml` names what the engine relies on.
- **Models.** `mlx-community/whisper-large-v3-turbo-asr-8bit` is the
  default model (0.87 GB of weights, 1.01 GB footprint after use);
  `-asr-fp16` is the catalogue's second (1.76 GB footprint, about 20 ms
  faster at finalize; both rate speed 2). The 8 bit beside Kokoro measured 1.75 GB, inside ADR
  0008's 3 GB for the two services; the fp16 would be about 2.5 GB (its
  footprint plus Kokoro's 0.74 GB, estimated, not measured together). The
  8 bit is the default for the room it leaves. Any other `whisper-large-v3-turbo` repository of
  mlx-community's is refused (`ModelRefused`): the plain one cannot load;
  a local checkpoint folder is not refused.
- **Only speech reaches the model.** Input that fails
  `voiced.audio.stt_input` (rate, shape, silence under -60 dBFS, under
  100 ms) is an empty transcript. The rest is cut by Silero
  (`voiced.vad.speech_spans`, the endpointer's threshold and 96 ms
  minimum) into spans of speech: a pause up to 2 s (`PAUSE_MS`) stays inside
  a span, a longer one separates two. Each span, with 0.3 s either side
  (`PAD_S`), is decoded on its own by mlx-audio's port of Whisper's
  long-form loop, and its segment times are offset back to the input's.
  Without a hint the first span's detected language is passed to the rest,
  even when that span was a burst of noise Silero took for speech; a hint
  avoids it.
  So silence never reaches the decoder, wherever it is: deciding
  all-or-nothing on the whole input let 40 s of room noise between two
  sentences come back as a third copy of the sentence, a stream's tail as
  "Turn on the básico", and Spanish after 35 s of noise as English, since
  Whisper detects the language on the first 30 s. Silero rejected every
  hallucinating input above and accepted every speech fixture, including
  speech at -30 dB and speech under -40 dBFS noise. It costs about 4 ms per
  second of audio, on the model thread. A span is one decode of at least
  the 30 s window's cost, so speech broken by several long pauses costs one
  decode per span.
- **Batch.** Word timestamps: the segments are Whisper's, their start and
  end set by their words.
- **Streaming.** A session is the shared
  `voiced.engines.buffered.BufferedStreamSession`, lifted from Qwen3-ASR as
  ADR 0027 planned: it buffers `add_audio`, joins the turn and hands it to
  the engine at `finalize`. The turn is decoded without word timestamps
  (they would add 0.45 s to the critical path), taking `voiced.gpu.stt_turn`
  for each span because a client waits (ADR 0019). The session runs the
  decode through `ModelThread.run_stoppable` with a stop flag of its own,
  set when the finalize is cancelled (the client hung up) or the session
  closed, and the engine checks it between spans; before, the flag was the
  session's closed state, which the transports set only after the cancelled
  finalize returned, so Qwen3-ASR's check between pieces never fired in
  production. A span already being decoded runs to its end. `partial()` is
  always `""`: a partial would cost a whole finalize on the one model
  thread, and a finalize asked for meanwhile would wait for it.
  `max_streams` is 1, and a batch request is refused while a session is
  open.
- **Languages.** `languages` are Whisper's 100 codes as BCP-47 primary
  subtags; Whisper's `jw` (Javanese) is `jv` both ways. A hint goes to the
  model, and the language it detects comes back.

## Alternatives considered

- **mlx-whisper.** The issue's suggestion, but it pulls in PyTorch and
  numba. Rejected.
- **Partials by decoding again.** Each about 0.5 s of GPU on the model
  thread, delaying the real finalize. Rejected, as for Qwen3-ASR.
- **Word timestamps on a stream's turn.** Twice the finalize. Rejected; a
  stream's segments come from Whisper's timestamp tokens.
- **Whisper's own no-speech check.** Its probability is always 0 in this
  port, and the hallucinations came with average log-probabilities above
  its threshold anyway. Silero instead.
- **Silero as a yes or no on the whole input, with Whisper's
  `hallucination_silence_threshold`.** The first version of this engine.
  On speech, 40 s of noise and speech again it wrote the sentence a third
  time, which the threshold did not catch, and it detected the language on
  noise. Rejected for spans.
- **One `generate` call with the spans as `clip_timestamps`.** Times come
  back absolute without offsets, but the call cannot be stopped or yield
  the GPU lock between spans. Rejected for one call per span.
- **fp16 as the default.** About 20 ms faster at finalize (510 / 527
  against 529 / 557 ms), the same speed rating, 0.75 GB more. Kept as the
  second model.

## Measurements

On an Apple M1 Max with 64 GB, 2026-10-21. `scripts/bench_engine.py`, the
service alone (ADR 0024; the rows in [benchmarks.md](../benchmarks.md);
both harnesses send `language="en"`):

| Model | WER | Finalize after 200 ms silence, median / worst | At the last word | Batch RTF | Footprint | MLX peak, load / after use | Ratings |
|---|---|---|---|---|---|---|---|
| 8 bit | 1.4% | 529 / 557 ms | 526 / 549 ms | 0.141 | 1.01 GB | 1.71 / 1.72 GB | quality 5, speed 2 |
| fp16 | 1.6% | 510 / 527 ms | 504 / 524 ms | 0.131 | 1.76 GB | 2.46 / 2.47 GB | quality 5, speed 2 |

The first version of the engine (no spans) measured the 8 bit at 1.6% and
533 / 851 ms, speed 1, on one 851 ms worst case that did not come back;
these rows replace it. Parakeet on the same harness: 2.0%, finalize 6 ms
worst after silence, batch RTF 0.023, 1.52 GB. On these 24 clean
LibriSpeech clips Whisper's word error rate is the lower one (1.4% against
2.0%); on the leaderboard's average over eight English sets it is the
higher (8.39 against 6.51), the better guide for English in general. The
time is what differs most: a worst finalize of 557 ms against 6 ms, and six
times Parakeet's batch real-time factor.

`scripts/bench.py` with the 8 bit and Kokoro up together, as they serve:

| Measure | Result | Target (ADR 0008) |
|---|---|---|
| Finalize, 64 ms chunks, 200 ms silence | 524 median / 542 ms worst | 50 / 150 ms |
| The same at the last word | 527 / 552 ms | 50 / 150 ms |
| The same, Kokoro synthesizing | 616 / 688 ms | 50 / 150 ms |
| Two clients at once (one stream each in turn) | 528 / 962 ms | 50 / 150 ms |
| Kokoro first audio while a client streams to Whisper | 113 median / 542 ms worst | 200 ms |
| Batch, 10.1 s clip over HTTP, word timestamps | 993 ms median | 300 ms |
| STT footprint, idle after use | 1.01 GB (MLX peak 1.72 GB) | |
| STT + Kokoro footprint, idle after use | 1.75 GB | 3 GB |

In process, warm, 8 bit, by turn length (one span each):

| Turn | Finalize, hint | Finalize, no hint | Batch (word timestamps), hint |
|---|---|---|---|
| 2.8 to 3.4 s | 485 to 500 ms | 918 to 1002 ms | 950 ms |
| 10 s | 530 ms | 1030 ms | 1026 ms |
| 30 s | 527 ms | 1023 ms | 1023 ms |

300 s of speech in one span took 60 s in batch with word timestamps and
31 s without, with a 1.77 GB MLX peak (measured before spans; one span is
one call, as then).

## Known misses

- Streaming finalize: 524 ms median and 542 ms worst with Kokoro idle,
  against ADR 0008's 50 and 150 ms (10 times the median, 3.6 times the
  worst); 616 / 688 ms while Kokoro synthesizes; about 0.92 s without a
  language hint (923 ms median in process). It does not grow with the turn
  up to 30 s of speech, but each extra span (a pause over 2 s) adds a
  decode. A voice assistant should keep Parakeet.
- No partial transcripts while a stream runs.
- Kokoro's first audio while a client streams to Whisper: 542 ms worst
  against its 200 ms (2.7 times; the median, 113 ms, meets it), on turns of
  one short span. Finalize holds the GPU lock for a whole span's decode
  (ADR 0019 lets STT go first): at least one 30 s window, about 0.5 s, and
  for longer continuous speech about 0.1 s per second of it (8.45 s for a
  70 s span, about 31 s for 300 s), which the TTS then waits out. The worst
  case is the longest span in the turn.
- Batch: 993 ms for 10 s of audio over HTTP against 300 ms (3.3 times),
  since word timestamps take a second pass; real-time factor 0.14 against
  Parakeet's 0.023.
- One stream at a time; a second client waits for the first's finalize
  (962 ms worst).
- A cancelled finalize stops between spans, not inside one: a span is one
  decode that runs to its end, about 0.1 s per second of continuous speech
  (8.45 s measured for a 70 s span before the next stream could start).
  Silero's pass over the turn, about 4 ms per second of it, cannot be
  stopped either; a finalize cancelled before it starts returns at once.
- Silero adds about 4 ms per second of turn to finalize (40 ms for 10 s).

## Consequences

- An engine on mlx-audio's Whisper port: `generate(language=,
  word_timestamps=)` and the result's `language` and `segments`. The fake
  tier runs the engine against stand-in `mlx` and `mlx_audio` modules with
  the real Silero on CI; the model tier checks load, batch with segments
  and detection, silence and hum, 40 s of noise between two sentences,
  Spanish after 35 s of noise, a stream against the batch transcript,
  unknown languages, the language table against the tokenizer's,
  `/v1/models` and a transcription over HTTP, and the fp16 model. The e2e
  suite subtitles Kokoro's speech through it.
- `BufferedStreamSession` serves both batch-only engines and owns the
  cancel; a third engine of the kind should also move the one-session slot
  (`_session`, `_require_idle`, `_session_closed`) into it.
- `voiced.vad.speech_spans` is there for any batch-only engine that writes
  words for silence.
- If mlx-audio's port gains a working no-speech probability, Silero could
  move from a gate to a second opinion; until then it is the only defence
  against "Thank you." on silence.
