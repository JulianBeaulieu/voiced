# 0009: Kokoro pronounces espeak-ng phonemes, not misaki's

Date: 2026-10-14. Status: accepted.

## Context

Kokoro-82M takes a phoneme string, not text. mlx-audio's `KokoroPipeline`
produces that string with misaki, a Python G2P that falls back to espeak only
for unknown words. During the model benchmarks misaki mangled ordinary
assistant output: "CO2" became "co two", and times and units came out wrong
in other ways. The ONNX port of Kokoro (`kokoro-onnx`) uses espeak-ng for
everything and pronounced the same sentences correctly, at the cost of
running on the CPU. Feeding espeak-ng's phonemes into the MLX model gave the
ONNX pronunciation at MLX speed: 92 ms per sentence, real-time factor 0.04,
in a standalone benchmark script.

Using `KokoroPipeline.generate_from_tokens` for that turned out to require
constructing a `KokoroPipeline`, which imports misaki (and with `misaki[en]`,
spaCy and a transformer model) even when its G2P is never called. mlx-audio
0.5 no longer installs misaki by default.

## Decision

`KokoroEngine` phonemizes with `kokoro_onnx.tokenizer.Tokenizer`, which wraps
espeak-ng through the `phonemizer` package and filters the result to
Kokoro's vocabulary, and calls the MLX model directly:

```python
language = KOKORO_LANGUAGES[voice[0]]  # "a" -> en-US / espeak "en-us", "b" -> en-GB / "en-gb"
phonemes = tokenizer.phonemize(sentence, language.espeak)
audio = model(phonemes, voice_pack[len(phonemes) - 1], speed, return_output=True).audio
```

Voice packs are loaded with mlx-audio's `load_voice_tensor` from the model
snapshot; all enabled packs are loaded and evaluated at `load()` so no
request pays for a first-use stall. misaki is not a dependency.

Text normalization (`voiced.text.normalize`) runs in the HTTP and Wyoming
layers before the engine sees the text, as `engines/base.py` specifies; the
engine only splits sentences. The normalizer spells acronyms ("CO2"
becomes "C O two") because espeak reads "CO2" as a word, and writes an
isolated capital "A" as "eh" because espeak reads it as the article.

Only the English voices (`af_*`, `am_*`, `bf_*`, `bm_*`) are exposed in
version 1. The voice table in `voiced/engines/kokoro.py` lists all 54 voices
and all nine languages with their espeak voice names. Enabling another
language takes three things: its tag in `KokoroEngine.languages`, a text
normalizer for it (`normalize(text, language=...)` accepts only `en` today
and raises for anything else), and for Japanese and Chinese a check that
espeak-ng's phoneme inventory matches what the model was trained on (misaki
uses different G2Ps for them).

## espeak-ng and the GPL

The Kokoro-82M weights are Apache-2.0. They are not committed to this
repository; `load()` downloads them from the Hugging Face repo
`mlx-community/Kokoro-82M-bf16` into the local cache on first run.

espeak-ng is GPL-3. voiced is MIT. espeak-ng is used as an external shared
library loaded at run time, never copied into this repository, so the
voiced code stays MIT and the user installs espeak-ng separately. There is
one documented way and one override, and `load()` fails with the install
command if neither works:

1. The Homebrew keg, `brew install espeak-ng`: `/opt/homebrew/opt/espeak-ng`
   on Apple Silicon, `/usr/local/opt/espeak-ng` on Intel.
2. `VOICED_ESPEAK_LIBRARY` (and optionally `VOICED_ESPEAK_DATA`) when set.
   A configured path that does not exist is an error naming the path.
   `voiced install` copies both variables from the installing shell into the
   daemons' plist environment, as it does `HF_HOME`.

Each candidate is self-tested by phonemizing one word before it is accepted.
The `kokoro-onnx` package also depends on the `espeakng-loader` wheel, which
bundles its own espeak-ng; the engine does not look for it, but it is used
when `VOICED_ESPEAK_LIBRARY` names its library, and its data directory is
what the tokenizer reads whenever `VOICED_ESPEAK_DATA` is not set.

### The wheel's espeak-ng and long paths

Added 2026-10-18. The wheel's espeak-ng keeps the data path in a 160-byte
buffer. When the resolved path of `espeak-ng-data` is longer than 159 bytes
it prints `Error processing file '/Users/runner/work/espeakng-loader/.../phontab'`
and calls `exit(1)`: the whole TTS process dies and no Python handler sees
it. A symlink to a short path does not help, because `phonemizer` resolves
the path. Inside a virtual environment the path is about 75 bytes plus the
user name at the default `~/.voiced`, but a custom `[install] venv_dir`, a
long home directory, or side-by-side virtual environments exceed it.
Homebrew's espeak-ng 1.52 has no such limit.

So when no data directory is configured, `load()` clones the wheel's data
with `cp -cR` (an APFS clone, no extra disk space) to
`<per-user cache dir>/voiced/espeak-ng-data-<espeakng-loader version>`, where
the cache directory is `confstr(_CS_DARWIN_USER_CACHE_DIR)`
(`/private/var/folders/../C/`, about 75 bytes, derived from the uid so it
works for daemons too). A complete copy (one with `phontab`) is reused and
never replaced, because a running service reads the dictionaries again
whenever it switches voice. A wheel upgrade gets a new directory, and a copy
the system purged is made again. Each load clones into its own staging
directory and renames it into place; when several load at once, the first
rename wins and the others use that copy. Staging directories left by a
crash are removed after an hour. Directories of older wheel versions are
kept, since a service started before the upgrade may still read one.

Before loading, every resolved data path is checked against 159 bytes
(Homebrew's is about 45). A longer one fails `load()` with `EspeakNotFound`
naming the path and the remedy (Homebrew's espeak-ng, or a
`VOICED_ESPEAK_DATA` copy at a short path) instead of letting espeak-ng exit
the process. A copy that cannot be made fails with `could not copy espeak-ng
data to <path>`, not as a missing espeak-ng.

Note that Homebrew's deprecated `espeak` formula conflicts with `espeak-ng`
(both install an `espeak` binary); `brew unlink espeak` first if it is
installed.

## Consequences

- Pronunciation quality depends on espeak-ng's English dictionaries, which
  are good for common words and acronyms and occasionally wrong for names.
  The normalizer is the place to fix systematic cases.
- `doctor` checks for the library with the engine's own
  `espeak_candidates()` and reports the path it would load (or the
  override), so an unlinked keg passes and an `espeak-ng` binary on `PATH`
  without the keg does not. It is a failure when the configured TTS engine
  is `kokoro`, a warning otherwise.
- Kokoro's 510-phoneme limit is enforced by the sentence splitter (300
  characters per chunk) and, because acronym-heavy text phonemizes to more
  than 1.7 characters per character, by cutting the phoneme string at spaces
  with the same `cut_to_length` helper.
- Measured 2026-10-14 on an M1 Max, one engine per process, warm:
  load 1.9 s including warm-up, first chunk 106 ms median, real-time factor
  0.046, 469 MB maximum resident set size, 326 MB active MLX memory.
