# 0021: Phonon ships selectable, not default, on the CPU

Date: 2026-10-20. Status: accepted.

## Context

Phonon is Gradium's family of small text-to-speech models: a flow-matching
transformer that predicts Mimi codec latents one 80 ms frame at a time, and
Mimi's decoder, which turns them into 24 kHz audio. The English model,
`gradium/phonon-31m-en`, has 31M parameters without the audio tokenizer and
its 8-bit weights (`model.q8.gguf`) are 64.5 MB. It runs in Gradium's Rust
runtime, `xn`, shipped in the `ptts` wheel: no PyTorch, no espeak-ng, its
own text normalization. The voices sounded good for their size, and a model
this small could leave the GPU to speech-to-text.

Checked on an Apple M1 Max (64 GB, macOS 15.7.4) with Python 3.12 before
building anything:

- `ptts` 0.4.0 has a `cp39-abi3-macosx_11_0_arm64` wheel of 6.5 MB whose only
  dependency is numpy. The quickstart synthesized.
- It downloads into the standard Hugging Face cache (it honours `HF_HOME`)
  and reads a local checkpoint folder without the Hub.
- `stream()` yields before the sentence is done: the first chunk came after
  21 to 25 ms warm (48 ms cold), and a 5.4 s sentence finished in about
  360 ms (14 to 16 times real time). Chunks are float32, 80 to 320 ms long.
- It runs on the CPU by default; the build also offers `metal`. During
  synthesis it kept about five of the ten cores busy. Peak RSS with the
  model loaded was 229 MB.
- An `AudioStream` can be advanced from any thread, and `next()` releases
  the GIL: an asyncio ticker on 5 ms sleeps saw at most 8.7 ms between
  ticks while chunks were read through `asyncio.to_thread`.

## Licences

| What | Licence | Source |
|---|---|---|
| `gradium/phonon-31m-en` (weights, tokenizer, default voice) | Apache-2.0 | model card, `license: apache-2.0` |
| `gradium/phonon-voices` (the preset voices) | Apache-2.0 | model card |
| `gradium/phonon-40m-en`, `-40m-multilingual` (and `-fr`, `-de`, `-es`, `-pt`) | CC-BY-NC-4.0, gated | model cards, `license: cc-by-nc-4.0` |
| `ptts`, the runtime | MIT OR Apache-2.0 | wheel metadata |
| Kyutai's Pocket TTS and Mimi, which Phonon builds on | CC-BY-4.0 | `kyutai/pocket-tts`, `kyutai/mimi` model cards |

Gradium releases Phonon 31M's weights, tokenizer and voices under
Apache-2.0, which allows commercial use. voiced redistributes none of them:
each Mac downloads them from Hugging Face. The 40M models are for
non-commercial use only and need their terms accepted on the Hub.

## Decision

`phonon` is a fourth TTS engine, `voiced.engines.phonon.PhononEngine`, a
`SentenceTTSEngine`, selectable and never the default. Kokoro stays the
default: it is the engine every other measurement and the Home Assistant
path were tuned on, and Phonon is new.

- **Dependency.** `ptts>=0.4,<0.5` in a `phonon` extra of its own (it needs
  no MLX, so `pip install voiced[phonon]` works alone), and in the shipped
  lock with `stt` and `tts`, so the app, Homebrew and `voiced install` all
  carry it (6.5 MB) and the menu bar app can switch to it. Everything that
  imports `ptts` runs inside `load()`.
- **CPU, not GPU.** The model is loaded with `device="cpu"`. It never
  touches Metal, so ADR 0019's lock does not apply to it: it neither waits
  for nor holds the GPU the STT goes first on, and Kokoro's yield points
  have no counterpart here.
- **Threads.** Each blocking step (the load, every `next()` of a sentence's
  stream and its `close()`) runs in a worker thread through `run_blocking`,
  like the other engines' model calls, so the event loop never waits and a
  cancelled request keeps the slot until the step returns. No dedicated
  model thread: `ptts` holds no thread-bound state.
- **Streaming within a sentence.** `SentenceTTSEngine` gains
  `_synth_chunks(sentence, voice, speed)`, a blocking generator of int16
  pieces; the loop yields each piece as it comes and closes the generator
  when the sentence ends or the client leaves. Its default yields
  `_synth_sentence` as one piece, so Kokoro and Soprano are unchanged.
  Phonon yields each decoded chunk, converted to int16, and closing it
  leaves `ptts`'s `with` block, which stops Phonon's workers. First audio
  is the first chunk, not the sentence.
- **Text.** `lang="en"`, `ptts`'s English normalization: voiced is English
  only, and the runtime requires a language. voiced has already normalized
  the text, so it mostly finds nothing to do.
- **Model and voices pinned.** `voiced.models.OPTIONAL_PINS` fixes the
  model revision and the voices repository's revision: the ones `ptts`
  0.4.0 names as its defaults (later commits on the Hub change only the
  model card). They are pinned like the defaults but left out of the app's
  manifest, which sums what a default install downloads. The engine
  declares `download_patterns()` (config, tokenizer, `model.q8.gguf`,
  default voice: 65.0 MB) and a new optional `extra_downloads()`, the voices
  repository with `voices.jsonl` and the 37 English presets (9.5 MB).
  `voiced install` fetches both with byte progress, `load()` reads them
  from the cache through `voiced.models.snapshot`, and
  `voiced uninstall --remove-models` removes their pinned revisions, and
  the 40M models', with the defaults' (every `OPTIONAL_PINS` entry is in
  `pinned_revisions()`, whichever engine the config names). `ptts` gets local
  folders only, the catalogue included, so it never reaches the Hub
  itself.
- **Voices.** `default`, the checkpoint's own voice, first (an engine switch
  without a voice picks it), then the English rows of `voices.jsonl` as a
  static table in the engine: the id is the catalogue's name as it is
  (`Easton`, `The Snow Queen`), the language its locale (`en-US`, `en-GB`,
  `en` for the character voices) and its gender, so the menu bar app labels
  them as it does Kokoro's. A static table lets the catalogue list voices
  without the cache (ADR 0013); a model test keeps it equal to the pinned
  `voices.jsonl`. Every preset is registered with `add_voice` at load, so
  a named voice is on disk before the first request, and a name outside
  the table is `UnknownVoice` before `ptts` sees it. Voice cloning needs
  Gradium's hosted API and is out of scope.
- **Speed.** Phonon has no rate control; `speed` is checked against the
  shared bounds and ignored, as for Soprano.

### The 40M models

`tts.model = "gradium/phonon-40m-en"` or `"gradium/phonon-40m-multilingual"`
works, at pinned revisions, and is never a default. The engine sets its
`license` from the model, so `/v1/models`, the status page and Wyoming's
info report `CC-BY-NC-4.0` for them and `Apache-2.0` for 31M. The pins mark
them gated. Without access, the download fails at load or at
`voiced install` with what to do: accept the licence on the model page,
then, as the user the services run as, run `hf auth login` with `HF_HOME`
set to the Hugging Face home the services use. The plists pass `HF_HOME`
and no `HF_TOKEN`, so the token has to be the `token` file there.
`voiced doctor` fails the "Model access" check when that file is missing
and prints the same command.

## Measurements

Apple M1 Max, 64 GB, macOS 15.7.4. `scripts/bench.py` with the new
`--phonon-url`, against `voiced serve stt` (Parakeet), `voiced serve tts`
(Kokoro) and a third `voiced serve tts` running Phonon, each in its own
process, over real sockets. First audio is the `openai` client's streamed
`pcm` request for "Turn on the kitchen lights." to the first byte, voice
`default`, after one warm-up.

| Metric | Measured |
|---|---|
| Phonon 31M first audio | median 44 / p95 50 / worst 51 ms (n=10) |
| Phonon 31M first audio while another client streams to STT | median 25 / p95 37 / worst 38 ms (n=30) |
| Kokoro first audio, same run, for comparison | median 103 / p95 106 / worst 107 ms (n=10) |
| Kokoro first audio while another client streams to STT, same run | median 142 / p95 282 / worst 351 ms (n=30) |
| STT streaming finalize, 64 ms chunks, 0 ms trailing silence, alone | median 100 / p95 106 / worst 111 ms (n=32) |
| The same while Phonon synthesizes back to back | median 109 / p95 124 / worst 134 ms (n=16) |
| The same with 200 ms trailing silence, Phonon synthesizing | median 1 / p95 2 / worst 2 ms (n=16) |
| TTS (Phonon 31M) memory after a long reply | footprint 0.26 GB, RSS 0.53 GB, no MLX allocations |
| Phonon 40M English first audio | median 40 / p95 46 / worst 47 ms (n=10) |
| TTS (Phonon 40M English) memory after a long reply | footprint 0.18 GB, RSS 0.41 GB |

Phonon does not wait for the STT's encoder calls, so its first audio does
not grow while someone speaks; it ran faster then, plausibly because the
CPU stays clocked up. Its CPU work costs the STT a little: the push-to-talk
finalize (0 ms trailing silence) rose by about 9 ms median and 23 ms worst,
inside the 150 ms target. The in-process engine measured first audio of 12
to 37 ms for the default voice, a preset and a character voice, and about
40 pieces for a two-sentence reply.

## Consequences

- A fourth engine to keep working. `ptts` is pre-1.0, so the pin is to its
  minor release; the fake tier runs the engine against a stand-in `ptts`
  on CI, and the model tier checks load, synthesis, piece order and timing,
  unknown voices, the voice table against `voices.jsonl`,
  `/v1/audio/voices` and `/v1/models`, and the 40M English model when a
  token is present. The e2e suite feeds Phonon's speech for two presets
  back through Parakeet.
- `SentenceTTSEngine._synth_chunks` is open to any engine that decodes a
  sentence in pieces.
- `extra_downloads()` joins `download_patterns()` and `catalogue_voices()`
  as an optional engine classmethod outside the frozen interface.
- Phonon's voices outside English (65 of the 102) and the other 40M
  languages wait for voiced's languages beyond English.
