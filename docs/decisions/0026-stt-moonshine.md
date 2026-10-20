# 0026: Moonshine ships selectable, not default, on the CPU

Date: 2026-10-20. Status: accepted.

## Context

Moonshine v2 Streaming is Moonshine AI's family of small streaming speech
recognisers: Tiny (34M), Small (123M) and Medium (245M). They encode audio as
it arrives and decode the open line every half second. They run in ONNX
Runtime on the CPU through the `moonshine-voice` package. The publisher
reports 18, 38 and 59 ms from end of phrase to text on a MacBook Pro, which
would be inside ADR 0008's window, and the models are small enough to sit
beside a TTS model without touching the GPU, which ADR 0019 gives to STT
first. Issue #58 asked whether it fits voiced.

Checked on an Apple M1 Max (64 GB, macOS 15.7.4) with Python 3.12 before
building anything:

- `moonshine-voice` 0.1.5 has one wheel, `py3-none-macosx_15_0_arm64`,
  17.7 MB. It bundles ONNX Runtime 1.23.2 and depends on numpy, requests,
  tqdm, filelock, platformdirs, google-crc32c and sounddevice; no PyTorch.
  `libmoonshine.dylib` is built with a minimum of macOS 15.0, so there is no
  way onto macOS 14, which voiced supports.
- It runs on the CPU only: Core ML is disabled upstream, and there is no MLX
  port of v2.
- **Finalize on demand works.** `Stream.stop()` runs a last pass over the
  audio added since the previous one and completes the open line, whatever
  Moonshine's own speech detector thinks. A forced update
  (`MOONSHINE_FLAG_FORCE_UPDATE`) decodes the same but leaves the line open.
  `decode_incomplete_lines=false` moves the whole decode into `stop()`
  (474 ms for Medium); `use_speculative_decoding` is already on, and off it
  doubles the cost. The defaults stay.
- A pass re-decodes the whole open line, so its cost grows with the phrase,
  and `add_audio` blocks for the length of a pass (90 ms for Tiny, 300 ms
  for Small, under load) whenever one is due.
- The library downloads from its CDN, `download.moonshine.ai`, into a
  platformdirs cache. The publisher mirrors that CDN on the Hub as
  `moonshine-ai/moonshine-voice-assets`, with the CDN's paths. The English
  streaming folders there match the library's download manifest file for
  file and byte for byte.
- **Word timestamps** need a second decoder,
  `decoder_kv_with_attention.ort` (32, 82 and 147 MB), and cost about 20 ms
  per finalize and 15 percent on batch. Batch word times are good. Streamed
  word times are not: in every line, every word after the first two to six
  carries the line's start time as both start and end.
- Any streaming architecture argument loads any streaming folder: the model
  reads its geometry from `streaming_config.json`. Tiny and Medium loaded as
  Small give the same transcripts, batch and streamed, as under their own
  architecture, so the engine passes one for all three.

## Licences

| What | Licence | Source |
|---|---|---|
| `moonshine-voice` (the runtime, Python and C++) | MIT | wheel metadata; repository `LICENSE` |
| English streaming Tiny, Small and Medium | MIT | `LICENSE` ("all streaming speech-to-text models and all English-language models"); the model cards `moonshine-ai/moonshine-streaming-{tiny,small,medium}`, `license: mit` |
| Streaming models for ar, de, es, ja, zh, vi and tl | contradictory | `LICENSE` and each `moonshine-ai/moonshine-streaming-*-<lang>` card say MIT; the asset mirror's README says every non-English model is under the non-commercial Moonshine Community License, and `get_model_for_language()` prints that warning for any language but English |
| ONNX Runtime | MIT | bundled in the wheel |

The non-English streaming runtime files (`quantized_26_08_24`) are only on
the CDN; the Hub mirror predates them. With no pinnable source and two
statements of their licence that disagree, voiced offers none of them. That
is consistent with the English-only decision anyway. voiced redistributes no
weights: each Mac downloads them from the Hub.

## Decision

`moonshine` is a second STT engine,
`voiced.engines.moonshine.MoonshineEngine`, selectable and never the
default. Parakeet stays the default: it is faster at finalize and on batch
and covers 25 languages (measurements below).

- **Dependency.** `moonshine-voice>=0.1.5,<0.2` in a `moonshine` extra,
  under the marker `sys_platform == "darwin" and platform_release >= "24"`
  (Darwin 24 is macOS 15). The extra is in the shipped lock, locked and
  hashed like the others; an install on macOS 14 skips the package, and
  the engine's `load()` says "the moonshine engine needs macOS 15 or
  later". `scripts/lock_target.py` evaluates markers for macOS 14 and 15:
  NOTICE lists what either installs, and the app's download size is the
  macOS 14 one plus what only 15 adds.
  Everything that imports `moonshine_voice` runs inside `load()`.
- **CPU, not GPU.** Every runtime call runs on the engine's one
  `ModelThread`, so the event loop never blocks and no two calls overlap.
  It never touches Metal, so ADR 0019's lock does not apply.
- **Streaming over the runtime's own stream.** A session creates a
  `Stream` with the runtime's 0.5 s update interval on its first audio.
  `add_audio` hands the samples to the stream on the model thread and
  waits for it, a pass included when half a second is due: that wait is the
  backpressure, with no queue in between (Parakeet's queue exists to rank
  six sessions on one thread; this engine has one). `partial()` is the text
  of the stream's lines so far, kept from its events. `finalize()` calls
  `stop()`. `stop()` reports a failed pass only to the stream's listeners
  and returns nothing, so a `None` from it fails the call, at load (the
  warm-up stream) and at finalize alike. Moonshine's own end of phrase only
  splits the session into lines.
- **Silence.** Batch input with no 32 ms frame above -60 dBFS is returned
  as an empty transcript without a pass, with Parakeet's gate
  (`voiced.audio.is_silent`).
- **Segments** are Moonshine's lines with their times, clipped to the input,
  in batch and streaming alike. Word timestamps are off: streamed word times
  are broken, and the attention decoder costs memory and finalize time for
  edges that move by tens of milliseconds.
- **`max_streams` is 1.** A second stream's pass on the one thread would
  delay the first one's finalize by a pass; nothing was measured that
  would justify more.
- **Models.** `model` names the publisher's model card id. Each is a
  catalogue entry (ADR 0022) of its own: the repository
  `moonshine-ai/moonshine-voice-assets` at one pinned revision, and
  patterns for its folder's 8 files (no attention decoder). The engine
  loads the pinned snapshot and reads the folder its patterns name:

  | `model` | Folder | Download |
  |---|---|---|
  | `moonshine-ai/moonshine-streaming-small` (default) | `model/small-streaming-en/quantized_26_08_21` | 142 MB |
  | `moonshine-ai/moonshine-streaming-tiny` | `model/tiny-streaming-en/quantized_26_08_21` | 45 MB |
  | `moonshine-ai/moonshine-streaming-medium` | `model/medium-streaming-en/quantized_26_08_21` | 269 MB |

  A local folder works too. Small is the default: the issue asked for it,
  and Tiny's accuracy (vendor average WER 12.0 against 7.84) is the price
  of its speed.
- **Languages**: English only.

Moonshine is the first engine whose models share one repository, so it is
the first to rely on the catalogue's ids over repositories:
`voiced models download` fetches one folder, `voiced models delete` deletes
the revision whole and so the other folders cached with it (it says so, and
refuses while one of them is in use), and `scripts/bench_engine.py` gives a
model's size from its locked files, not from the repository's revision.

## Measured

On the M1 Max, macOS 15.7.4, on a quiet machine: no other benchmark, model
test or voiced service ran (load average 1.5 to 3.8). Each model served by
`voiced serve stt` beside Kokoro on `voiced serve tts`, measured with
`scripts/bench.py`; CPU from `ps` every 0.5 s, in the window where both
processes were busy (STT streaming while Kokoro synthesized):

| Metric | Tiny | Small | Medium | ADR 0008 target |
|---|---|---|---|---|
| Finalize, 200 ms trailing silence, 64 / 100 ms chunks, 32 alignments each: median, worst | 49 / 52, 80 / 73 ms | 66 / 65, 131 / 140 ms | 124 / 122, 202 / 208 ms | 50 ms median, 150 ms worst |
| Finalize, 0 ms trailing silence (push-to-talk), 64 / 100 ms chunks: median, worst | 59 / 61, 87 / 86 ms | 105 / 107, 162 / 156 ms | 171 / 168, 211 / 220 ms | 150 ms worst |
| Finalize while Kokoro synthesizes, 200 / 0 ms trailing silence: median, worst | 48 / 66, 64 / 90 ms | 68 / 119, 134 / 168 ms | 116 / 200, 204 / 233 ms | 150 ms worst |
| Finalize with 2 clients at once (`stt.streams` 2, `max_streams` 1), 200 / 0 ms: median, worst | 50 / 63, 374 / 370 ms | 74 / 111, 584 / 649 ms | 135 / 183, 949 / 893 ms | 150 ms worst |
| Kokoro first audio while a client streams to Moonshine: median, worst | 106, 129 ms | 106, 136 ms | 108, 141 ms | 200 ms |
| Batch, 10.1 s clip, median | 545 ms | 1065 ms | 1526 ms | 300 ms |
| Footprint idle after use, STT alone and with Kokoro | 0.17, 0.92 GB | 0.33, 1.07 GB | 0.47, 1.22 GB | 3 GB for STT and TTS |
| CPU in the Kokoro window: STT mean and peak, Kokoro mean, percent of one core | 97, 119; 90 | 99, 119; 129 | 101, 120; 159 | |

Kokoro alone gave its first audio in 94 to 95 ms median, 97 to 99 ms worst.
Over a whole run, the STT process averaged 15 percent of one core.

Without the server, the runtime alone (`Stream.stop()` after the 3.4 s
clip streamed in 20 ms chunks, 7 runs each), and the same for one phrase of
6 and 10 s:

| Model | Finalize, 3.4 s phrase | 6 s phrase | 10 s phrase | Batch, 10 s | RSS after load |
|---|---|---|---|---|---|
| Tiny | median 62, worst 66 ms | 72 ms | 41 to 45 ms | 501 ms | 177 MB |
| Small | median 104, worst 107 ms | 79 to 96 ms | 84 to 85 ms | 982 ms | 482 MB |
| Medium | median 148, worst 151 ms | 134 to 146 ms | 141 to 145 ms | 1417 ms | 789 MB |

What finalize costs is one pass over the audio since the previous pass, up to
half a second of it, plus a decode of the open line: a phrase whose end
falls just after a pass finalizes in a few milliseconds (3 ms for each model
at exactly 3.0 s). Under load from other work on the machine (load average 7
to 9), every number above roughly doubled: Small 160 ms median, 223 ms worst.
Accuracy on the fixture was word for word for all three models.

`scripts/bench_engine.py` (ADR 0024), on the same quiet machine, each model
by its own service with nothing beside it; the rows are in
[docs/benchmarks.md](../benchmarks.md), Parakeet's for comparison:

| Model | Quality | Speed | WER | Finalize after 200 ms silence, median / worst | At the last word, median / worst | Batch RTF | Footprint | Size |
|---|---|---|---|---|---|---|---|---|
| Tiny | 3 | 4 | 7.0% | 50 / 81 ms | 62 / 88 ms | 0.044 | 0.20 GB | 45.2 MB |
| Small | 4 | 3 | 3.8% | 74 / 170 ms | 108 / 165 ms | 0.124 | 0.32 GB | 142.3 MB |
| Medium | 4 | 3 | 3.4% | 120 / 212 ms | 178 / 207 ms | 0.114 | 0.47 GB | 269.1 MB |
| Parakeet | 5 | 5 | 2.0% | 1 / 6 ms | 115 / 129 ms | 0.023 | 1.52 GB | 2.5 GB |

The medians agree between the two harnesses. The worst cases are single
alignments out of 64 and move between runs: Small's after 200 ms of silence
was 135, 211 and 170 ms in three runs, Medium's 442 and 212 ms in two.

## Known misses

- **50 ms median finalize: missed by Small and Medium.** Tiny meets it after
  200 ms of trailing silence (49 to 52 ms; 50 ms in the `bench_engine.py`
  row) and misses it at the last word (59 to 62 ms). Small's median is 65
  ms with 200 ms of trailing silence and 105 ms for push-to-talk. Parakeet's
  is 1 ms, because its speculative final call runs during the silence
  (ADR 0008); the runtime has no such call, and running a pass on silence
  costs as much as the one at `end`.
- **150 ms worst finalize: missed by Small and Medium.** Small meets it
  with 200 ms of trailing silence in one run (140 ms) and misses it in the
  other (170 ms); it misses it for push-to-talk (156 to 165 ms) and while
  Kokoro synthesizes at the last word (168 ms). Medium misses it in every
  case (202 to 233 ms). Tiny is inside it everywhere (at most 90 ms).
- **Batch: missed.** 545 ms (Tiny), 1065 ms (Small) and 1526 ms (Medium) for
  10 s of audio against 300 ms. The runtime decodes autoregressively on the
  CPU; nothing in voiced shortens it.
- **One stream at a time.** A second client waits for the first session to
  end: 370 to 949 ms worst with two clients. For Small that is 584 and 649
  ms, against 365 and 448 ms in an earlier version that queued the waiting
  audio and handed it to the stream in one call; this one hands it over
  chunk by chunk, as it arrives.
- **macOS 14** cannot run it. The engine and its three models are still
  listed there (the module imports without the runtime, and the catalogue
  does not know the platform), so the app offers them: a download of 45 to
  269 MB succeeds, and the service then fails at `load()` with "the
  moonshine engine needs macOS 15 or later". Each model's note says
  "macOS 15+". Hiding it below macOS 15 is a small change left for
  [#85](https://github.com/JulianBeaulieu/voiced/issues/85): an optional
  `available()` classmethod that the listing reads the way it reads
  `catalogue_voices`.
- **The model tier needs macOS 15.** CI's manual `model-tests` job runs on
  macos-14 by default, where the package is not installed and the Moonshine
  tests skip; run it with a macOS 15 runner to cover them.
- **The vendor's figures** (38 ms for Small) are measured from their own
  speech detector's end of phrase with audio fed faster than real time, not
  from a client's end signal; they do not compare with voiced's.

## Alternatives considered

- **Moonshine's own CDN download.** No revision voiced can pin, no shared
  Hugging Face cache, and no `voiced uninstall --remove-models`.
- **Ending the turn on Moonshine's line completion.** voiced ends turns on
  the client's `end` or Wyoming's `AudioStop` (ADR 0008); a second, earlier
  end would cut users off mid-pause.
- **A forced update instead of `stop()`.** Same cost, and the line stays
  open, so the transcript would have to be completed by hand.
- **Leaving the extra out of the shipped lock.** It would keep macOS 14
  installs working, but the app and Homebrew could then never switch to it.
  The marker does both.
