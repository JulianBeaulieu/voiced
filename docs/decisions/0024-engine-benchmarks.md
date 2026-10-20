# 0024: Engine benchmarks and model ratings

Date: 2026-10-20. Status: accepted.

## Context

voiced has four engines (Parakeet, Kokoro, Soprano, Phonon), seven more are
being written and five more are planned. Someone choosing between them needs
the same numbers for each, measured the same way, and a short verdict in the
model list, as cjpais/Handy shows an accuracy bar and a speed bar per model.
Today the numbers are spread over the README, ADRs 0008, 0010, 0015 and 0021,
measured by hand with different clips, counts and machines. Every engine's
author must be able to produce its row with one command.

Handy (`src-tauri/src/catalog/catalog.json`, `catalog/mod.rs`,
`ModelCard.tsx`) keeps per model a name, a one-line description, repository,
revision, licence, parameters, languages, capabilities and a curated
recommended rank; measured results (error rate per language on FLEURS, a
real-time factor per backend, one reference machine) sit beside them, and two
0 to 1 scores are computed from those by fixed formulas
(`100·e^(−error/15)`, `100·(1 − e^(−rtf/8))`). The card shows the two bars,
the size, the description and a Recommended badge. voiced takes the shape:
measured numbers in the repository, ratings derived from them by fixed
rules, a note and the recommendation chosen by hand. It does not take the
curve formulas (a step table is easier to check by eye), the per-language
FLEURS sweep (later, below) or the editorial ranking (the defaults are the
recommendation).

## Decision

### 1. One harness, one command per engine

`scripts/bench_engine.py`, beside `scripts/bench.py`, whose measuring
functions it imports, and like it a standalone script: the `openai` client
and the measurement code stay off the daemon's path. `bench.py` keeps its
job, the assembled system with two services at once (ADR 0015).

```sh
uv run python scripts/bench_engine.py stt --engine parakeet
uv run python scripts/bench_engine.py tts --engine kokoro [--model <repo>]
uv run python scripts/bench_engine.py render
```

A run starts `voiced serve <kind>` itself on free ports, with a config of its
own and no `VOICED_` variable of the caller's shell, so two people get the
same service. `--url` measures a service started by someone else (bench.py's
way), without its load time. A TTS run also starts the default STT service,
to stream to (`--stt-url` to use another). Each run prints its Markdown row
and the ratings with what they came from, writes
`docs/benchmarks/<kind>-<engine>-<model>-<hardware>.json` (a rerun of the
same model on the same hardware replaces it) and regenerates
`docs/benchmarks.md` from every result file. A test renders the result
files and fails if `docs/benchmarks.md` differs, as `scripts/refresh_locks.py`'s
check does for the locks: the document and the harness cannot drift. One file
per result, not one shared table, so engines benchmarked in parallel branches
do not conflict; a conflict in the generated document is resolved by running
`render`.

### 2. The protocol

Common to both kinds:

- **Load**: the service is started twice; the second start is timed from
  spawn to `/health` answering 200, so it includes the Python imports and the
  engine's warm-up but not a download.
- **Memory** from the service's `/metrics.json` 1 s after use, as the README
  reports it: the physical footprint (what Activity Monitor and memory
  pressure count, Metal buffers included) and MLX's peak allocation since
  start (Parakeet's load peak included); "none" for an engine without MLX.
- **Size**: the bytes of the model's revision in the Hugging Face cache (the
  pinned one, else `main`'s), without repositories shared between models
  (Phonon's voices, about 9.5 MB).
- **Languages and licence** from the engine class (`languages`, `license`).

Speech to text:

- **Accuracy**: word error rate of batch transcription (`language="en"`)
  over 24 LibriSpeech test-clean utterances (CC BY 4.0), fixed by id and
  sha256 in `CLIPS`. Four female and four male speakers, the first eight in
  the archive's order that balance the two; per speaker the first three
  utterances by id of 3 to 12 s whose transcript has no number words: 172.5
  s, 443 words. They are streamed out of the archive at bench time and kept
  in `~/.cache/voiced/bench` (3.3 MB); the read stops after the last one, 80
  MB into the 346 MB file. Nothing is committed. WER is corpus-level, every
  error over every reference word.
- **Throughput**: the batch real-time factor, transcription time over audio
  time, the 24 clips uploaded one by one after a warm-up.
- **Streaming finalize**, ADR 0008's method with `bench.py`'s functions: the
  3.4 s fixture streamed in real time in 64 ms chunks at 32 lead offsets 20
  ms apart, median and worst from `end` to the `final` event, once after
  200 ms of trailing silence (the endpointer's window, the default path) and
  once after none (push to talk). An engine that does not stream gets "n/a".

Text to speech, voice `tts.default_voice` if the engine serves it, else its
first voice (`--voice` to choose):

- **First audio** for "Turn on the kitchen lights.", from the request to the
  first PCM byte through the `openai` client: 10 requests after a warm-up,
  and 30 while a second client streams speech to the STT service (`bench.py`'s
  two rows, ADR 0019).
- **Throughput**: the real-time factor of `bench.py`'s long reply (four
  sentences, about 20 s of audio), the median of three.
- **Naturalness**: UTMOS, below.
- **Sample rate**: every engine serves 24 kHz mono 16-bit PCM (the audio
  convention); an engine whose model runs at another rate resamples (Soprano
  from 32 kHz), so the document states the rate once, not per row.

**Naturalness is scored, not judged.** UTMOS22 (the "strong learner" of the
VoiceMOS 2022 winner, MIT) predicts a 1 to 5 mean opinion score. It runs on
Apple Silicon on the CPU through tarepan/SpeechMOS's port, which needs only
`torch` and `torchaudio`: `scripts/utmos_score.py` runs in a throwaway
environment that `uv run --isolated --with torch==2.14.1 --with
torchaudio==2.11.0` builds, so torch never enters voiced's lock, and fetches
the model through `torch.hub` at a fixed commit (tag v1.2.0). The score is
the mean over three fixed sentences (`NATURALNESS_TEXTS`). What was tried:

| Candidate | Result |
|---|---|
| `utmos` 1.1.10 on PyPI (unofficial) | depends on `fairseq` 0.12.2, which builds from source, and on `cached-path`, which pulls boto3; not pursued |
| UTMOSv2 (sarulab-speech) | not on PyPI; a git install with timm, librosa and transformers; not pursued |
| SpeechMOS UTMOS22 via `torch.hub` | works. First run downloads torch (about 15 minutes on a slow link, cached by uv afterwards); then 15 s for three files. `torchaudio.load` needs `torchcodec` from torchaudio 2.9 on, so the script reads WAV with the standard library |

As an anchor, three of the clip set's human recordings score 3.73, 4.36 and
4.23. A scorer that cannot run (no network on a first run) leaves the score
and the quality rating empty and says why; `--no-naturalness` skips it.

### 3. Machines, commit, date

Every result names its machine as `system_profiler` and `sw_vers` report it,
model name, chip, memory and macOS version (never a host name):
"MacBook Pro, Apple M1 Max, 64 GB, macOS 15.7.4" for the laptop the engines
are developed on, "Mac Studio, Apple M1 Max, 32 GB, macOS 27.0.1" for the
server. It also names the commit (`-dirty` when `src/`, `pyproject.toml` or
`uv.lock` had uncommitted changes) and the date. Numbers from different
machines are different rows. The ratings in the model list come from the
laptop's rows, the one machine every engine is measured on; a Studio row is
shown in the document only.

### 4. Ratings

Quality and speed, each 1 to 5, from fixed limits (a value on a limit gets
the better rating), so the same table gives anyone the same ratings. The
limits are constants in the harness, and the document's Ratings table is
generated from them.

| Rating | Measure | 5 | 4 | 3 | 2 | 1 |
|---|---|---|---|---|---|---|
| STT quality | word error rate on the clip set | <= 3% | <= 5% | <= 8% | <= 12% | otherwise |
| STT speed | finalize after 200 ms of silence, worst | <= 50 ms | <= 150 ms | <= 300 ms | <= 600 ms | otherwise |
| STT speed | batch real-time factor | <= 0.05 | <= 0.1 | <= 0.2 | <= 0.5 | otherwise |
| TTS quality | UTMOS naturalness | >= 4.3 | >= 4 | >= 3.6 | >= 3 | otherwise |
| TTS speed | first audio, median | <= 100 ms | <= 200 ms | <= 400 ms | <= 800 ms | otherwise |
| TTS speed | real-time factor | <= 0.1 | <= 0.25 | <= 0.5 | <= 1 | otherwise |

Speed is the lower of a kind's two ratings: the latency a user waits on and
whether the engine keeps up (a TTS engine slower than real time stalls
playback whatever its first audio). An STT engine without streaming is rated
on throughput. The limits were set before the first results, from the
project's targets (ADR 0008's 150 ms worst finalize; 200 ms first audio,
ADR 0015) and from public ranges (human read speech scores about 4.0 to 4.3
on UTMOS22; current English ASR models score 2 to 8 % WER on test-clean). The
finalize rating uses the 200 ms case because it is the default path (Home
Assistant and the endpointer); the push-to-talk case is in the table.

### 5. Where the ratings live, and who shows them

- **docs/benchmarks.md**: one table per kind, generated, every measured
  number, the ratings per row, the limits and how to regenerate. The README's
  "Benchmarks" keeps a short summary of the defaults and links to it; no
  table is kept twice.
- **The catalogue** (ADR 0022's `CATALOGUE` in `voiced/models.py`): each
  `Model` gains `quality: int | None`, `speed: int | None` and `note: str`,
  a one-line description by hand ("The default: accurate and fast English
  and 24 European languages"). Quality and speed are copied from the
  laptop's row, and a test derives them again from that row's result file
  with the harness's `rate()` and fails on a difference, naming the
  command that measures the model. A model without a laptop row has none.
  Size, languages and licence are already ADR 0022's (the lock's bytes, the
  engine class, the catalogue's licence). The recommended badge is the
  install default of each kind, not a new field.
- **`voiced models list --json`** adds `quality`, `speed` and `note` to each
  model, and **the app's model rows** (ADR 0022 section 5) show two five-step
  bars labelled Quality and Speed, the note, and "Recommended" on the
  default; the bars link to docs/benchmarks.md.

`CATALOGUE` and the model rows do not exist yet: ADR 0022's implementation is
in progress. The catalogue fields and their test, the `models list` fields
and the app's bars are follow-up changes after it lands, in that order.

## Alternatives considered

- **jiwer for WER.** It computes the alignment, but its default transform
  only strips and lowercases; the normalization that matters (punctuation,
  hyphens, apostrophes, accents) would still be ours. Our normalizer and the
  word Levenshtein distance are 40 lines with tests, and no new dependency.
- **A number normalizer** (Whisper's English one converts number words and
  digits both ways). A reference like "EIGHTEEN FIFTY FIVE" against "1855"
  is formatting, not recognition, and a reliable normalizer is several
  hundred lines; the clip set leaves out transcripts with number words.
- **Measuring engines in-process** instead of through `voiced serve`. Simpler
  timing, but it would not see the service's own costs (decode, queue,
  HTTP) and the memory would include the harness.
- **One shared results table** edited by each run. Parallel engine branches
  would conflict on every merge.
- **A listening protocol** (three sentences, three listeners, 1 to 5) for
  naturalness. UTMOS runs unattended on any Mac; a listening panel cannot be
  part of one command. It stays the way to settle a case where UTMOS and
  listeners disagree.
- **Handy's continuous scores.** Bars from `e^(−error/15)` look precise but
  hide the limits; integer steps from a printed table are what "two people
  get the same rating" needs.

## Consequences

- Adding an engine adds a step to CONTRIBUTING: commit the engine, run the
  harness for each of its models, commit the result files and the document.
- The clip set is English only. A multilingual set is later: FLEURS (CC BY
  4.0) has the languages Parakeet v3 and Nemotron claim, but needs its own
  per-language normalization; LibriSpeech's Multilingual sibling is CC BY 4.0
  too. When it comes, quality becomes per language, as Handy's is.
- STT accuracy is measured on read audiobook speech in batch; streaming
  accuracy and noisy far-field speech are not measured.
- UTMOS predicts the opinion of listening-test panels; it rewards clean
  studio-like speech and can misjudge voices unlike its training data. Its
  number is a guide, the column says UTMOS, and the rating says quality.

## Measured

On 2026-10-21, MacBook Pro, Apple M1 Max, 64 GB, macOS 15.7.4, commit
67aa19f, one engine run at a time under the machine's shared benchmark lock.
The rows are in [benchmarks.md](../benchmarks.md). An STT run takes 4.5
minutes (the streaming rows are real time), a TTS run under a minute once
the scorer's environment is cached.

| Engine | Quality | Speed | From |
|---|---|---|---|
| Parakeet | 5 | 5 | WER 2.0 %, finalize worst 2 ms, batch RTF 0.024 |
| Kokoro | 5 | 5 | UTMOS 4.52, first audio 96 ms median, RTF 0.043 |
| Phonon 31M | 5 | 5 | UTMOS 4.50, first audio 32 ms median, RTF 0.042 |
| Soprano | 5 | 4 | UTMOS 4.35, first audio 126 ms median, RTF 0.079 |

- Parakeet's 9 word errors in 443: 6 are spelling or formatting (British
  "travellers" and "colour", "we're" for "we are" and "Neoplatonists" for
  "neo platonists", two errors each), 1 is LibriSpeech's (the reference has
  "with" where the speaker says "which"), 2 are recognition errors. A
  British spelling map is a candidate for the normalizer; until then the WER
  limits leave that room.
- All three TTS engines score above the human recordings' anchor and within
  0.17 of each other, so all three rate 5 for quality; the limits stay as
  set before the runs, and the UTMOS column keeps the difference visible.

Against the README's earlier figures (same laptop):

| Figure | Earlier | Now | Why |
|---|---|---|---|
| Kokoro first audio | 96 ms median, 112 worst | 96 median, 99 worst | same method; the worst varies run to run |
| Push-to-talk finalize | 114 median, 165 worst | 101 median, 123 worst | the earlier figure pooled 64 and 100 ms chunks; the protocol uses 64 ms |
| Finalize after 200 ms of silence | 1 median, 16 worst | 1 median, 2 worst | likewise |
| Batch, 10 s of audio | 253 ms (one tiled clip) | RTF 0.024, about 240 ms | 24 clips of 3 to 12 s instead of one clip tiled |
| STT + Kokoro footprint idle | 2.21 GB | 1.55 + 0.73 = 2.28 GB | each service alone now, measured after its own run |
| Phonon first audio | 28 median, 33 worst | 32 median, 38 worst | run to run |
| Parakeet MLX peak | 3.6 to 4.1 GB at load | 3.60 GB | same |

The README now gives the new figures and links to the document.
