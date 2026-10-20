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
own (only `[install] hf_home`, the caller's Hugging Face home, so the service
uses the models already downloaded) and no `VOICED_` variable of the caller's
shell, so two people get the same service. It warns when other voiced
services answer on the default ports or hold the GPU lock. A TTS run also
starts the default STT service, to stream to (`--stt-url` to use another).
Each run prints its Markdown row and the ratings with what they came from.
Only a complete run is saved: `--url` (a service started by someone else,
bench.py's way: no load time, and this Mac is not the one to name; the served
engine and model must match the ones named), `--no-naturalness` and
`--no-save` print only, and a scorer that cannot run fails the run. A saved
run writes
`docs/benchmarks/<kind>-<engine>-<model>-<hardware>.json` (a rerun of the
same model on the same hardware replaces it) and regenerates
`docs/benchmarks.md` from every result file. A test renders the result
files and fails if `docs/benchmarks.md` differs, as `scripts/refresh_locks.py`'s
check does for the locks: the document and the harness cannot drift. One file
per result, not one shared table, so engines benchmarked in parallel branches
do not conflict; a conflict in the generated document is resolved by running
`render`.

Result files are read back field by field from the dataclasses' types: a
field added later must have a default, and then every file written before it
stays valid; a field the reader does not know is ignored. The ratings and
their limits are `voiced.ratings` (standard library only), so the package
and the harness rate the same way.

### 2. The protocol

Common to both kinds:

- **Load**: the service is started twice; the second start is timed from
  spawn to `/health` answering 200, so it includes the Python imports and the
  engine's warm-up but not a download.
- **Memory** from the service's `/metrics.json`: the physical footprint
  (what Activity Monitor and memory pressure count, Metal buffers included)
  1 s after use, before the naturalness scorer runs, and MLX's peak
  allocation twice: the load peak, right after the service is ready, and
  the peak since start, after use. Where serving stays under the load peak
  the two are equal, and the serving peak is the one the engine's ADR
  measured in process (Parakeet's 2.46 GB, ADR 0008). "none" for an engine
  without MLX allocations.
- **Size**: the bytes of the model's revision in the Hugging Face cache (the
  pinned one, else `main`'s), without repositories shared between models
  (Phonon's voices, about 9.5 MB).
- **Languages and licence** from the engine class (`languages`, `license`).

Speech to text:

- **Accuracy**: word error rate of batch transcription (`language="en"`)
  over 24 LibriSpeech test-clean utterances (Panayotov et al. 2015, CC BY
  4.0, openslr.org/12), fixed by id and by the sha256 of the audio and of
  the transcript in `CLIPS`. Four female and four male speakers, the first eight in
  the archive's order that balance the two; per speaker the first three
  utterances by id of 3 to 12 s whose transcript has no number words: 172.5
  s, 443 words. They are streamed out of the archive at bench time and kept
  in `~/.cache/voiced/bench` (3.3 MB); the read stops after the last one, 80
  MB into the 346 MB file. Nothing is committed. WER is corpus-level, every
  error over every reference word, after lower-casing and removing accents
  and punctuation, splitting at hyphens and spelling out Mr., Mrs. and Dr.
  as LibriSpeech does.
- **Throughput**: the batch real-time factor, transcription time over audio
  time, the 24 clips uploaded one by one after a warm-up.
- **Streaming finalize**, ADR 0008's method with `bench.py`'s functions: the
  3.4 s fixture streamed in real time in 64 and 100 ms chunks at 32 lead
  offsets 20 ms apart each, median and worst over the 64 from `end` to the
  `final` event, once after 200 ms of trailing digital silence (the
  endpointer's window, the default path) and once after none (push to
  talk). ADR 0008's noise-floor and utterance-length rows are not repeated.
  Every STT engine streams (`STTEngine.stream` is abstract), so a streaming
  failure fails the run.

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
the model's code through `torch.hub` at a fixed commit (tag v1.2.0) and its
weights checked against a sha256. The score is
the mean over three fixed sentences (`NATURALNESS_TEXTS`). What was tried:

| Candidate | Result |
|---|---|
| `utmos` 1.1.10 on PyPI (unofficial) | depends on `fairseq` 0.12.2, which builds from source, and on `cached-path`, which pulls boto3; not pursued |
| UTMOSv2 (sarulab-speech) | not on PyPI; a git install with timm, librosa and transformers; not pursued |
| SpeechMOS UTMOS22 via `torch.hub` | works. First run downloads torch (about 15 minutes on a slow link, cached by uv afterwards); then 15 s for three files. `torchaudio.load` needs `torchcodec` from torchaudio 2.9 on, so the script reads WAV with the standard library |

As an anchor, three of the clip set's human recordings score 3.73, 4.36 and
4.23. A scorer that cannot run (no network on a first run) says why and
fails the run; `--no-naturalness` skips it and saves nothing.

### 3. Machines, commit, date

Every result names its machine as `system_profiler` and `sw_vers` report it,
model name, chip, memory and macOS version (never a host name):
"MacBook Pro, Apple M1 Max, 64 GB, macOS 15.7.4" for the laptop the engines
are developed on, "Mac Studio, Apple M1 Max, 32 GB, macOS 27.0.1" for the
server. It also names voiced's version, the commit of `main` the checkout
branches from (`git merge-base HEAD origin/main`, which survives a squash
merge), with `+` when the measured code (`src/`, `scripts/`,
`pyproject.toml`, `uv.lock`) differs from it, as it does on the branch that
adds an engine; "unknown" outside a git checkout. And the date. Numbers from different
machines are different rows. The ratings in the model list come from the
laptop's rows, the one machine every engine is measured on; a Studio row is
shown in the document only.

### 4. Ratings

Quality and speed, each 1 to 5, from fixed limits (a value on a limit gets
the better rating), so the same table gives anyone the same ratings. The
limits are `voiced.ratings.SCALES`, the document's Ratings table is generated
from them, and a test holds them to this table.

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
playback whatever its first audio). The limits were set before the first results, from the
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
  `Model` gains three fields, all with defaults so a model without
  benchmarks stays valid:

  ```python
  quality: int | None = None  # 1 to 5, from the reference row; None: not measured
  speed: int | None = None  # 1 to 5, likewise
  note: str = ""  # one line by hand, at most 80 characters
  ```

  The reference row of a model is the result file in `docs/benchmarks/`
  whose `kind` is its engine's kind, whose `engine` and `model` are the
  catalogue's, and whose `hardware` field is "MacBook Pro, Apple M1 Max,
  64 GB". `quality` and `speed` are written by hand from that row, and
  `tests/core/test_models.py` gains one test that, for every catalogue
  model, reads the result files as JSON (it does not import the script),
  finds the reference row, and asserts that `(quality, speed)` is
  `voiced.ratings.rate()` of that row's numbers on their scales: for stt
  `metrics.wer_percent` on `STT_WER`, `metrics.finalize_after_silence.worst_ms`
  on `STT_FINALIZE` and `metrics.batch_rtf` on `STT_RTF`; for tts
  `metrics.utmos` on `TTS_UTMOS`, `metrics.first_audio.median_ms` on
  `TTS_FIRST_AUDIO` and `metrics.rtf` on `TTS_RTF` (the pairs
  `bench_engine.rated()` builds); or both None when there is no reference
  row; its failure names the
  command that measures the model. A shared repository (`Engine.shared`) has
  no ratings. The note says what the model is for ("The default: accurate
  and fast, English and 24 European languages"). Size, languages and licence
  are already ADR 0022's (the lock's bytes, the engine class, the
  catalogue's licence). The recommended badge is the install default of each
  kind (`PINS`' models today, the catalogue's default engine's first model
  then), not a new field.
- **`voiced models list --json`** adds `"quality"`, `"speed"` (integers or
  null) and `"note"` (a string, possibly empty) to each model object, beside
  ADR 0022's fields, and `"recommended": true` on the install defaults. The
  text view adds a `Quality` and a `Speed` column (`4/5`, or `-`).
- **The app's model rows** (ADR 0022 section 5) show under the name two
  five-step bars labelled Quality and Speed (no bars when null), the note,
  and a Recommended badge; the bars' help text is "Measured on an Apple M1
  Max; see docs/benchmarks.md", linking to the document on GitHub.

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

On 2026-10-21, MacBook Pro, Apple M1 Max, 64 GB, macOS 15.7.4, voiced 0.1.1
at a9b3c96 plus this change, one engine run at a time with nothing else
running. The rows are in [benchmarks.md](../benchmarks.md). An STT run takes
about 8 minutes (the streaming rows are real time), a TTS run under a minute
once the scorer's environment is cached.

| Engine | Quality | Speed | From |
|---|---|---|---|
| Parakeet | 5 | 5 | WER 2.0%, finalize worst 6 ms, batch RTF 0.023 |
| Kokoro | 5 | 5 | UTMOS 4.52, first audio 94 ms median, RTF 0.042 |
| Phonon 31M | 5 | 5 | UTMOS 4.50, first audio 27 ms median, RTF 0.038 |
| Soprano | 4 | 5 | UTMOS 4.25, first audio 97 ms median, RTF 0.071 |

- Parakeet's 9 word errors in 443: 6 are spelling or formatting (British
  "travellers" and "colour", "we're" for "we are" and "Neoplatonists" for
  "neo platonists", two errors each), 1 is LibriSpeech's (the reference has
  "with" where the speaker says "which"), 2 are recognition errors. A
  British spelling map is a candidate for the normalizer; until then the WER
  limits leave that room.
- Soprano samples its tokens (temperature 0.3), so its audio and its scores
  differ from run to run. An earlier run of the same code gave UTMOS 4.35
  (quality 5) and first audio 126 ms median, 392 ms worst; this one 4.25
  (one sentence at 3.90) and 97 ms. A rating near a limit can move by one
  between runs of such an engine; the committed row is the last run.
- Kokoro and Phonon score above the human recordings' anchor and within
  0.02 of each other; the limits stay as set before the runs.

Against the figures the README gave before (same laptop; each earlier figure
from the ADR named):

| Figure | Earlier | Now | Why it differs |
|---|---|---|---|
| Kokoro first audio | 96 ms median, 112 worst (ADR 0015) | 94 median, 98 worst | same method; the worst varies run to run |
| Kokoro first chunk | 106 ms median (ADR 0009) | 94 ms median | in process with multi-sentence text then; over HTTP with one short sentence now |
| Kokoro real-time factor | 0.046 (ADR 0009) | 0.042 | in process then; the long reply over HTTP now |
| Kokoro memory | 469 MB peak resident, 326 MB MLX active (ADR 0009) | 0.73 GB footprint, MLX peak 0.76 GB at load | resident set and MLX's idle allocation then; the footprint, which counts Metal buffers, and the peak now |
| Kokoro MLX peak while serving | 2.16 GB (ADR 0015) | 1.86 GB after use | another workload: ADR 0015's run kept Kokoro synthesizing the long reply back to back through its finalize rows; here it is first-audio requests, the long reply three times and three short sentences |
| Kokoro load | 1.9 s (ADR 0009) | 1.8 s | engine load and warm-up then; spawn to ready now |
| Soprano first chunk | 156 to 223 ms median (ADR 0010) | 97 median, 156 worst | in process with ADR 0010's own requests then; over HTTP with one 27-character sentence now (Soprano generates a sentence's tokens before its first audio, so a short sentence is faster), and Soprano varies run to run (above) |
| Soprano memory | 392 MB resident (ADR 0010) | 0.39 GB footprint | the same size by another measure; the footprint counts Metal buffers |
| Parakeet batch | 163 ms for a 9.4 s clip (ADR 0008); 253 ms for 10.1 s tiled (ADR 0015) | RTF 0.023, about 230 ms per 10 s | 24 real clips of 3 to 12 s, uploaded one by one |
| Parakeet load | 1.2 s (ADR 0008) | 1.0 s | engine load and warm-up then; spawn to ready now, warm disk cache both |
| Parakeet memory | 1.28 GB after load, 2.46 GB serving peak, 3.6 to 4.1 GB load peak (ADR 0008) | 1.52 GB footprint after use, 3.38 GB MLX peak | MLX allocations in process then; the footprint after use and the peak since start now. The 3.38 GB peak is the load peak, under ADR 0008's 3.6 to 4.1 GB range; serving stays under it (section 2) |
| Finalize after 200 ms of silence | 1 median, 16 worst (ADR 0015) | 1 median, 6 worst | same chunks and alignments; the worst varies run to run |
| Push-to-talk finalize | 114 median, 165 worst (ADR 0015) | 115 median, 129 worst | same chunks and alignments; the worst varies run to run |
| STT + Kokoro footprint idle | 2.21 GB (ADR 0015) | 1.52 + 0.73 = 2.25 GB | each service alone now, measured after its own run |
| Phonon first audio | 28 median, 33 worst (ADR 0021) | 27 median, 31 worst | run to run |
| Phonon footprint | 0.26 GB (ADR 0021) | 0.26 GB | same |

The README now gives the new figures and links to the document.
