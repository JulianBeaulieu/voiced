# Architecture decision records

One file per non-obvious choice, numbered in the order they were made.
Status is `accepted` unless the record says otherwise. Add a new record with
the next free number; collisions from parallel branches are renumbered at
merge, and this index is updated when a record lands on `main`.

| ADR | Title | Summary |
|---|---|---|
| [0001](0001-project-structure.md) | Project structure | One package, two services (`stt`, `tts`), the module map, MLX engines as optional extras, port assignments. |
| [0002](0002-engine-interfaces.md) | Engine interfaces | Frozen async `STTEngine`, `STTStreamSession` and `TTSEngine` classes, fixed audio formats, entry-point discovery, error and cancellation rules. |
| [0003](0003-launchd-label-prefix.md) | launchd label prefix | `io.github.julianbeaulieu.voiced` as the single-constant prefix, with a planned switch to `computer.voiced`. |
| [0004](0004-launchd-daemon-vs-agent.md) | LaunchDaemons, not LaunchAgents | Measured evidence that Metal works from a daemon running as a normal user, what a non-root user may do with `launchctl`, the plist contents. |
| [0005](0005-shared-inference-and-audio-primitives.md) | Shared inference and audio primitives | `inference()` as the one way into an engine, the outcome taxonomy, timeouts on every socket await, rules for streaming transports, background model load (amended 2026-10-21: a hang-up ends synthesis at the engine's next step). |
| [0006](0006-install-layout.md) | Install layout | `~/.voiced` for the service environment, the printed root step, uninstall safety rules (amended by 0017: `~/.voiced` is the state directory; amended 2026-10-20: `--remove-models` deletes the pinned revisions through `huggingface_hub`). |
| [0007](0007-http-api.md) | HTTP API layer | ffmpeg as an external dependency, every speech format streaming, upload and input limits, the `model` field, the WebSocket protocol, the status page. |
| [0008](0008-stt-engine-parakeet.md) | Parakeet speech-to-text engine | `parakeet-mlx` streaming with a (64, 64) context and 0.8 s calls, the speech-start gate and speculative final call, Silero VAD via `pysilero-vad`, measured latency, memory and known limits. |
| [0009](0009-tts-kokoro-espeak-tokens.md) | Kokoro pronounces espeak-ng phonemes | espeak-ng phonemes into the MLX model instead of misaki, how espeak-ng is found, the GPL-3 implication. |
| [0010](0010-tts-soprano.md) | Soprano, selectable and not default | Apache-2.0 license check, the repetition penalty and per-sentence bound against runaway generation, 32 to 24 kHz resampling, first-audio and runaway measurements. |
| [0011](0011-wyoming.md) | Wyoming listeners | In-process listeners, STT utterance bounds, per-sentence TTS slots, error codes, zeroconf only when reachable, what Home Assistant expects. |
| [0012](0012-watchdog.md) | Watchdog design | Stdlib-only third daemon, the decision rules, stall detection, restart by SIGKILL plus `kickstart` as a non-root user. |
| [0013](0013-local-control-channel.md) | Local control channel | The watchdog serves a 0600 Unix socket with a peer uid check; settable keys, comment-preserving atomic config writes, restart-required rules, the opt-in browser form, protocol versioning. |
| [0014](0014-menu-bar-app.md) | Menu bar app | Native SwiftUI `MenuBarExtra` plus a Settings window in `menubar/`, speaks only the control socket, how it finds it, macOS 14 floor, a Swift fake control server for tests. Amended by 0018: the app installs the daemons and runs `launchctl` only in its signed privileged script. |
| [0015](0015-integration-results.md) | Integration results | End-to-end tier and benchmark (a script, not a subcommand), memory measured as the physical footprint, the measured table, defects found and fixed, known misses. |
| [0016](0016-verification-results.md) | Verification results | What an independent check of security, robustness, the quickstart, dependencies and repository hygiene found, what changed and which limits stay documented. |
| [0017](0017-homebrew-packaging.md) | Homebrew packaging and the shared install layout | The state directory every install path shares (`bin/voiced` link, `envs/`, `install.json`, identical plists, one lock, the watchdog following a replaced environment); a tap formula built from the lock; why not `brew services` or resource blocks; the menu bar cask as the end-user path. |
| [0018](0018-install-from-the-menu-bar-app.md) | Install from the menu bar app | Proposed. The app installs voiced into ADR 0017's shared layout (`~/.voiced/bin/voiced` symlink, `envs/<id>`, `install.json`) with a bundled uv and the hash-pinned lock, updates side by side with rollback, gets ffmpeg and espeak-ng from PyPI wheels, and installs the user-run LaunchDaemons through one administrator prompt for a script compiled into the signed executable that takes only the user name and renders fixed plists; SMAppService rejected; permissions asked only when needed. |
| [0019](0019-stt-first-on-the-gpu.md) | Speech-to-text first on the GPU | Why STT finalize slowed while Kokoro synthesized (encoder round trips queue behind whole command buffers), a per-user `flock` the STT holds per encoder call, Kokoro submitting block by block with two blocks in flight and a 300 ms wait budget, the measured variants and the first-audio cost. |
| [0020](0020-concurrent-stt-streams.md) | Concurrent STT streams | Several stream states over one Parakeet model, interleaved on the one model thread by rank (a call a client waits on, a session behind, the rest in turn), the finalize tail as one call, shared queue slots and `stt.streams` (2) from the measured finalize latency; batching and a thread per stream rejected. |
| [0021](0021-tts-phonon.md) | Phonon, selectable and not default | Phonon through `ptts` on the CPU, beside the GPU the other models share; each sentence streamed as Phonon decodes it; model and English presets pinned and fetched through `voiced.models`; Apache-2.0 31M as the model, the CC-BY-NC-4.0 gated 40M English model allowed but never the default (amended by ADR 0022: pins in `models.lock.json`, the multilingual model not catalogued); `ptts` threads capped at 4. |
| [0022](0022-models-and-engines-on-demand.md) | Models and engines on demand from the app | Accepted; the CLI, the catalogue and lock, the offline services and the app's model rows are in. A hand-written catalogue (`CATALOGUE` in `voiced.models`: repositories, licences, gated, patterns, shared repositories) and a generated `models.lock.json` (revisions, files, hashes, earlier pins); every catalogued engine's packages stay in the one lock until the first torch engine; model rows with Download, Delete and Use in the Speech to Text and Text to Speech panes, through `voiced models list\|download\|delete`; deletes only voiced's pinned revisions through `huggingface_hub` (the shared blob store); the token in the keychain; services never download, a missing model is the `missing_model` state, and an unknown voice falls back so Home Assistant keeps speaking. |
| [0023](0023-text-pipeline-languages.md) | Text is normalized for the voice's language | Accepted. `normalize` requires the voice's language; `check_voice` returns the served `Voice`; rewrites into words only for English, other languages keep digits; `split_sentences` takes no language: `。！？` (no space needed), `؟`, `।` and `॥` end sentences everywhere, closers stay with them, an integer before a period never ends one (German ordinals), a decimal does; one `primary_subtag`. |
| [0024](0024-engine-benchmarks.md) | Engine benchmarks and model ratings | `scripts/bench_engine.py` measures one engine with one command against a service it starts; STT word error rate on 24 fixed LibriSpeech test-clean clips, streaming finalize per ADR 0008, throughput; TTS first audio alone and while STT streams, throughput, UTMOS22 naturalness in a throwaway torch environment; load, memory, size; one JSON result per model and machine, `docs/benchmarks.md` generated from them and checked by a test; quality and speed ratings 1 to 5 by fixed limits, for the catalogue and the app's model rows. |
| [0028](0028-tts-pocket.md) | Pocket TTS, selectable and not default | Kyutai's English Pocket TTS through mlx-audio on the GPU, not `ptts` (stock `ptts` cannot load its checkpoints) and not MLX on the CPU (slower than real time); every 80 ms frame streamed, behind ADR 0019's gate; one model thread for the sentence's MLX state; model, tokenizer and the six commercially usable voices pinned; English only. |
| [0029](0029-tts-chatterbox-turbo.md) | Chatterbox Turbo, selectable and not default | Chatterbox Turbo through mlx-audio on the GPU, 4-bit by default, fp16 allowed, both pinned; voiced's own loop over T3's tokens and S3Gen renders (first 15 tokens, then every 25), waiting for STT before each GPU step; one voice, the bundled `default`; the cloning tokenizer mlx-audio fetches unpinned is refused for the load; no watermark in the MLX port; MIT, though the conversions are labelled Apache-2.0. |

## Writing one

```markdown
# NNNN: Title

Date: YYYY-MM-DD. Status: accepted | proposed | superseded by NNNN.

## Context
What forced a decision.

## Decision
What was chosen, concretely.

## Alternatives considered
What else was on the table and why it lost.

## Consequences
What becomes easier, harder, or must be remembered.
```

Keep it short. A record is a note to a future reader who wonders "why is it
like this", not a design document.
