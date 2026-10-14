# 0004: Kokoro pronounces espeak-ng phonemes, not misaki's

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
in the prototype.

Using `KokoroPipeline.generate_from_tokens` for that turned out to require
constructing a `KokoroPipeline`, which imports misaki (and with `misaki[en]`,
spaCy and a transformer model) even when its G2P is never called. mlx-audio
0.5 no longer installs misaki by default.

## Decision

`KokoroEngine` phonemizes with `kokoro_onnx.tokenizer.Tokenizer`, which wraps
espeak-ng through the `phonemizer` package and filters the result to
Kokoro's vocabulary, and calls the MLX model directly:

```python
phonemes = tokenizer.phonemize(sentence, "en-gb" if voice.startswith("b") else "en-us")
audio = model(phonemes, voice_pack[len(phonemes) - 1], speed, return_output=True).audio
```

Voice packs are loaded with mlx-audio's `load_voice_tensor` from the model
snapshot; all enabled packs are loaded and evaluated at `load()` so no
request pays for a first-use stall. misaki is not a dependency.

Text normalization (`voiced.tts.normalize`) runs before phonemization and
spells acronyms itself ("CO2" becomes "C O two"), because espeak reads
"CO2" as a word. An isolated capital "A" is written "eh" because espeak
reads it as the article.

Only the English voices (`af_*`, `am_*`, `bf_*`, `bm_*`) are exposed in
version 1. The voice table in `voiced/engines/kokoro.py` lists all 54 voices
and all nine languages with their espeak voice names; a language is enabled
by adding its tag to `KokoroEngine.languages`. Japanese and Chinese would
also need a check that espeak-ng's phoneme inventory matches what the model
was trained on (misaki uses different G2Ps for them).

## espeak-ng and the GPL

espeak-ng is GPL-3. voiced is MIT. espeak-ng is used as an external shared
library loaded at run time, never copied into this repository, so the
voiced code stays MIT and the user installs espeak-ng separately. The engine
looks for it in this order and fails at `load()` with the install command if
none works:

1. `VOICED_ESPEAK_LIBRARY` and `VOICED_ESPEAK_DATA` (or the `phonemizer`
   package's `PHONEMIZER_ESPEAK_LIBRARY` / `PHONEMIZER_ESPEAK_DATA_PATH`).
2. The Homebrew keg, `brew install espeak-ng`: `/opt/homebrew/opt/espeak-ng`
   on Apple Silicon, `/usr/local/opt/espeak-ng` on Intel.
3. Whatever `ctypes.util.find_library("espeak-ng")` finds.
4. The copy inside the `espeakng-loader` wheel that `kokoro-onnx` depends on.

The wheel copy is listed last for a practical reason: espeak-ng has a fixed
size path buffer and silently falls back to its compiled-in data path when
the data directory's path is long, which is what happens inside a deeply
nested virtual environment. The Homebrew data lives at a short path and does
not have the problem. The installer should still create the venv at a short
path (HANDOFF section 6) for the same reason.

Note that Homebrew's deprecated `espeak` formula conflicts with `espeak-ng`
(both install an `espeak` binary); `brew unlink espeak` first if it is
installed.

## Consequences

- Pronunciation quality depends on espeak-ng's English dictionaries, which
  are good for common words and acronyms and occasionally wrong for names.
  The normalizer is the place to fix systematic cases.
- `doctor` must check for a working espeak-ng (the engine's `find_espeak`
  helper can be reused).
- Kokoro's 510-phoneme limit is enforced by the sentence splitter (300
  characters per chunk) and, as a last resort, by splitting the phoneme
  string at spaces.
