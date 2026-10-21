# 0030: OmniVoice ships selectable, not default, for its languages

Date: 2026-10-20. Status: accepted.

## Context

OmniVoice (k2-fsa, `k2-fsa/OmniVoice`) is a multilingual text-to-speech
model. Its model card tags 646 languages, dialect tags among them
(Levantine Arabic `apc`, Egyptian `arz`, Gulf `afb` and more), the widest
coverage of any open model we surveyed. It is a masked-diffusion model: a
bidirectional Qwen3-0.6B backbone fills in a whole utterance's audio tokens
(8 codebooks, 25 frames a second) over N refinement steps, with
classifier-free guidance (two forward passes a step), and the HiggsAudio v2
codec decodes them to 24 kHz audio.

A first trial used the publisher's PyTorch package on MPS: 2.4 to 2.6 s per
sentence at the default 32 steps, 1.1 to 1.3 s at 16, no audio before the
sentence is done, about 2 GB of memory and a 4.1 GB install with PyTorch.
voiced does not take PyTorch as a dependency, and the latency is 6 to 12
times the 200 ms first-audio target, so it was set aside. It is back for its
languages alone: voiced's other engines speak English.

mlx-audio, which the `tts` extra already pins (0.5.9), has an `omnivoice`
module: the same weights on MLX, the codec's decoder included, no PyTorch
(voice cloning's encoder would need it; voiced does not clone). Checked on
an Apple M1 Max (64 GB, macOS 15.7.4) before building:

- It loads `k2-fsa/OmniVoice` as published (float32, 2.45 GB) through
  `mlx_audio.tts.utils.load_model` and runs on the GPU. Cast to float16 at
  load, the weights take 1.59 GB.
- Warm, through mlx-audio's `generate`, for sentences of 2.2 to 4.8 s of
  speech in English, German, Levantine Arabic, Spanish, French and
  Mandarin: 1.5 to 2.4 s each at 32 steps, 0.8 to 1.2 s at 16. MLX's peak
  during synthesis was 2.2 to 2.8 GB.
- `generate` yields once per call, after the last step and the decode: no
  audio before the sentence is done. The model fills every frame of the
  utterance at once, so there is nothing earlier to yield.
- Every sentence, at 32 and at 16 steps, transcribed back exactly
  (Parakeet for English, German, Spanish and French; Whisper large-v3-turbo
  for Levantine Arabic and Mandarin, detected as Arabic and Chinese at 0.99
  and above).
- mlx-audio's port does none of upstream's post-processing: sentences
  started with 0.4 to 0.8 s of silence and peaked at up to full scale.

## Licences

Read on the pages themselves at the pinned revision:

| What | Licence | Source |
|---|---|---|
| OmniVoice weights | CC-BY-NC, "due to constraints from its training data (e.g., Emilia)" | `k2-fsa/OmniVoice` README, "License" section; the card has no `license:` field |
| HiggsAudio v2 codec (`audio_tokenizer/`, needed to decode) | Boson Higgs Audio 2 Community License, based on the Meta Llama 3 Community License | `audio_tokenizer/LICENSE` in the same repository |
| OmniVoice code | Apache-2.0 | github.com/k2-fsa/OmniVoice |
| mlx-audio | MIT | already a dependency |

The codec licence asks anyone who makes the materials, or a product using
them, available to display "Built with Higgs Materials licensed from Boson
AI USA, Inc. ... and Meta Llama 3 licensed under the Meta Llama 3 Community
License", to keep Llama 3's acceptable use policy, not to use the materials
or their output to improve another large language model, and to ask Boson
AI for an expanded licence above 100,000 annual active users. voiced
redistributes neither the weights nor the codec (each Mac downloads them
from Hugging Face), and its README carries the attribution and both terms.
The `mlx-community` conversions are tagged `apache-2.0` on the Hub; that tag
is wrong, the original card's licence applies.

## Decision

`omnivoice` is a sixth TTS engine, `voiced.engines.omnivoice.OmniVoiceEngine`,
a `SentenceTTSEngine`, selectable and never the default.

- **Runtime.** mlx-audio's port, already in the `tts` extra: no new package,
  no extra. Everything that imports MLX runs inside `load()`.
- **Model.** `k2-fsa/OmniVoice`, the publisher's own repository, whose card
  states the licence: the `omnivoice` engine's one model in
  `voiced.models.CATALOGUE`, pinned in `models.lock.json` (ADR 0022). Its
  patterns are the config, the weights, the text tokenizer and
  `audio_tokenizer/` (the codec and its licence): 3.27 GB. The service loads
  it from the cache and never downloads it; `voiced models download
  k2-fsa/OmniVoice` does. The weights are cast to float16 before they are
  read (a lazy load), so the float32 checkpoint is never resident: float16
  was faster than bfloat16 on the M1 in the trial. An `mlx-community`
  conversion would halve the download, but it is a third party's copy under
  a wrong licence tag: the engine refuses any other model id
  (`ModelRefused`), as Phonon refuses uncatalogued Phonon models.
- **Steps.** 16, not upstream's 32, provisionally. Upstream's docs name 16
  for faster synthesis, it halves the time to first audio, and all six
  languages' sentences transcribed the same as at 32. Nobody has listened
  to the difference yet: the comparison is by transcription only, so a loss
  in naturalness that a listener would hear is possible. Each voice's
  sentence rendered at 16 and at 32 steps is kept for a listening check
  by the maintainer, and that check decides whether 16 stays; until then it
  is provisional. Eight steps clipped the onset of Arabic in the trial, so
  16 is not pushed lower. The count is a constant in the engine, as
  Phonon's quantization is: there is no per-engine option in the config to
  put it in.
- **The generation loop.** The engine calls mlx-audio's
  `iterative_unmask` itself rather than `generate`, about twenty lines, for
  two things `generate` does not offer: a GPU yield point before every
  forward pass, and `speed`. It reuses the model's own tokenization and
  input layout and mlx-audio's duration estimate.
- **GPU.** Synthesis holds the GPU for 0.8 to 1.2 s a sentence. Before
  each forward pass the engine waits for the STT's encoder calls
  (`voiced.gpu.Yielder`, ADR 0019, within its 0.3 s budget a sentence) and
  evaluates each pass on its own, so an STT call waits behind at most one
  pass, about 35 ms, not a whole sentence.
- **A client that leaves.** A sentence is one call on the engine's model
  thread, and a cancelled request waits for the call in flight (ADR 0005),
  so a client that left mid-sentence would hold the GPU and the slot for the
  rest of it: up to seconds at a low `speed`. `SentenceTTSEngine` now calls
  the engine's `_interrupt()` when the request is cancelled during a step
  (`ModelThread.run_stoppable`); OmniVoice's sets the sentence's stop event,
  which the next forward pass checks, so the GPU is free within a pass.
  Running each sentence on a thread of its own and polling it from the
  model thread instead was measured and rejected: the push-to-talk STT
  finalize while OmniVoice synthesized rose from 126 to 230 ms worst
  (n=96 each, alternating, same commit), for no reason found.
- **Speed.** Upstream applies `speed` by dividing the estimated length the
  model fills; the engine does the same on mlx-audio's estimate (1.15 times
  the rule-based estimate, at least 10 frames).
- **Post-processing.** Each sentence's edge silence quieter than -50 dBFS is
  cut to 0.1 s, and the sentence scaled to a 0.5 peak: upstream's
  `remove_silence_edges` and peak normalization, in numpy. Upstream also
  shortens pauses over 0.5 s inside a sentence; a sentence of a spoken
  reply rarely has one, so the engine does not. A sentence without a letter
  or a digit ("...") is skipped, as for Phonon: the model would fill its
  0.4 s minimum with sound.
- **Reproducible.** Each sentence starts from the same random seed, so the
  same text in the same voice gives the same audio.
- **No streaming within a sentence.** The engine yields one piece per
  sentence. First audio is the whole first sentence. This misses ADR 0008's
  200 ms target by a factor of about four, and nothing in this engine can
  fix that: it is the model's design.

### Languages and voices

The engine's `languages` are the 646 tags of the card's `language:` list at
the pinned revision, in its order (a model test reads the card as YAML and
compares them). The card quotes two of them, `no` (Norwegian) and `yes`
(Yeskwa), which YAML would otherwise read as booleans; the tags are without
the quotes. `/v1/models`, Wyoming's info and the voice listing serve those
of them that `tts.languages` names, as for every engine, so the default
`["en"]` serves the English voice only.

OmniVoice has no preset voices. Without a prompt it picks a random speaker
every sentence; with `instruct` it designs one from attributes (gender, age,
pitch, an English accent or a Chinese dialect). The engine ships six
designed voices, the attribute strings pinned in the engine, one per
language:

| id | Language (model tag) | `instruct` |
|---|---|---|
| `en-US-female` (first, the voice an engine switch picks) | `en-US` (`en`) | `female, young adult, american accent` |
| `de-DE-male` | `de-DE` (`de`) | `male, middle-aged` |
| `apc-female` | `apc` (`apc`) | `female, young adult` |
| `es-ES-male` | `es-ES` (`es`) | `male, young adult` |
| `fr-FR-female` | `fr-FR` (`fr`) | `female, middle-aged` |
| `zh-CN-male` | `zh-CN` (`zh`) | `男，青年` |

The model's language tag is the voice's primary subtag. The Mandarin
voice's attributes are in Chinese because upstream rewrites them that way
for Chinese text, and mlx-audio passes the string as it is. Accents
work only for English and dialects only for Chinese ("voice design is
trained on Chinese and English data only"); for the other languages the
attributes set gender and age and the language tag does the rest. The
voice's name is its age attribute, so the app labels it "Young adult
(Levantine Arabic, female)". A voice keeps its attributes from sentence to
sentence, but not a fixed timbre: the model designs the speaker afresh for
each sentence. A consistent speaker would need a reference clip (voice
cloning), which is later, as is a voice from an arbitrary attribute string.
The catalogue gains display names for `de-DE` and `apc`.

### Text in other languages

The transports normalize the text for the voice's language before the
engine sees it (ADR 0023): a German voice's "20" stays "20", which the model
reads in German, and Chinese and Arabic text is split at `。！？` and `؟`,
so a paragraph is synthesized sentence by sentence, not in one long pass.
The engine normalizes nothing itself.

## Measurements

Apple M1 Max, 64 GB, macOS 15.7.4, nothing else running, two harnesses on
the same commit.

`scripts/bench_engine.py tts --engine omnivoice` (ADR 0024) measures the
engine alone against a service it starts; its row is in
`docs/benchmarks.md`: first audio 797 ms median, 806 ms worst; 873 ms
median and 960 ms worst while another client streams to STT; real-time
factor 0.29; UTMOS 4.42; 2.13 GB footprint. Rated quality 5, speed 2.

`scripts/bench.py` with `--extra-tts OmniVoice=URL,en-US-female`, against
`voiced serve stt` (Parakeet), `voiced serve tts` (Kokoro) and a third
`voiced serve tts` running OmniVoice at 16 steps, each in its own process,
over real sockets. First audio is the `openai` client's streamed `pcm`
request for "Turn on the kitchen lights." to the first byte, voice
`en-US-female`, after one warm-up.

| Metric (`bench.py`) | Measured |
|---|---|
| OmniVoice first audio | median 778 / p95 794 / worst 797 ms (n=10) |
| OmniVoice first audio while another client streams to STT | median 880 / p95 941 / worst 952 ms (n=30) |
| Kokoro first audio, same run, for comparison | median 97 / p95 100 / worst 100 ms (n=10) |
| STT streaming finalize, 64 ms chunks, 0 ms trailing silence, alone | median 111 / p95 122 / worst 127 ms (n=32) |
| The same while OmniVoice synthesizes back to back | median 105 / p95 116 / worst 125 ms (n=16) |
| The same with 200 ms trailing silence, OmniVoice synthesizing | median 1 / p95 1 / worst 2 ms (n=16) |
| TTS (OmniVoice) memory after a long reply | footprint 2.14 GB, RSS 0.46 GB, MLX active 1593 MB, peak 3193 MB |
| STT (Parakeet) memory, same run | footprint 1.73 GB, MLX peak 3624 MB (while loading) |
| STT + TTS (OmniVoice) footprint, idle after use | 3.87 GB, against the 3 GB budget |
| STT + TTS (Kokoro) footprint, idle after use, for comparison | 2.48 GB |

Two targets are missed, and nothing in this engine can meet them:

- **First audio** misses ADR 0008's 200 ms by about four times, and by
  more while someone speaks, because the engine yields to every STT
  encoder call. It is the model's design: a whole sentence is synthesized
  at once.
- **Memory.** Beside the STT service the pair takes 3.87 GB, 0.87 GB over
  the 3 GB budget for both services (ADR 0008), and MLX
  peaks at 3.2 GB in the TTS process alone while it synthesizes. The
  float16 weights are 1.59 GB of it; the rest is the codec and the
  buffers of a sentence's passes. A Mac that runs OmniVoice needs that
  headroom.

The STT keeps its own target: the push-to-talk finalize while OmniVoice
synthesizes back to back is the finalize alone within noise (125 against
127 ms worst), because the engine waits for every encoder call and runs
each forward pass on its own.

## Consequences

- A sixth engine to keep working, on private parts of mlx-audio's port (the
  model's tokenization and input helpers, `iterative_unmask`'s keywords and
  the duration estimator, named in `pyproject.toml`'s pin comment); the pin
  to mlx-audio's minor release and the model tier guard them. The fake tier
  runs on CI without MLX.
- The menu bar app lists the six voices and their languages through
  `engines.list` like any engine's; the 646 languages go there too.
- The licence string, `CC-BY-NC; codec: Boson Higgs Audio 2 Community
  License`, is what `/v1/models`, the status page and Wyoming report.
- The first non-English voices in voiced, ahead of its other languages.
