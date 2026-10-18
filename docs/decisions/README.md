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
| [0005](0005-shared-inference-and-audio-primitives.md) | Shared inference and audio primitives | `inference()` as the one way into an engine, the outcome taxonomy, timeouts on every socket await, rules for streaming transports, background model load. |
| [0006](0006-install-layout.md) | Install layout | `~/.voiced` for the service environment, staged plists and printed sudo scripts, uninstall safety rules (amended by 0017: `~/.voiced` is the state directory). |
| [0007](0007-http-api.md) | HTTP API layer | ffmpeg as an external dependency, every speech format streaming, upload and input limits, the `model` field, the WebSocket protocol, the status page. |
| [0008](0008-stt-engine-parakeet.md) | Parakeet speech-to-text engine | `parakeet-mlx` streaming with a (64, 64) context and 0.8 s calls, the speech-start gate and speculative final call, Silero VAD via `pysilero-vad`, measured latency, memory and known limits. |
| [0009](0009-tts-kokoro-espeak-tokens.md) | Kokoro pronounces espeak-ng phonemes | espeak-ng phonemes into the MLX model instead of misaki, how espeak-ng is found, the GPL-3 implication. |
| [0010](0010-tts-soprano.md) | Soprano, selectable and not default | Apache-2.0 license check, the repetition penalty and per-sentence bound against runaway generation, 32 to 24 kHz resampling, first-audio and runaway measurements. |
| [0011](0011-wyoming.md) | Wyoming listeners | In-process listeners, STT utterance bounds, per-sentence TTS slots, error codes, zeroconf only when reachable, what Home Assistant expects. |
| [0012](0012-watchdog.md) | Watchdog design | Stdlib-only third daemon, the decision rules, stall detection, restart by SIGKILL plus `kickstart` as a non-root user. |
| [0013](0013-local-control-channel.md) | Local control channel | The watchdog serves a 0600 Unix socket with a peer uid check; settable keys, comment-preserving atomic config writes, restart-required rules, the opt-in browser form, protocol versioning. |
| [0014](0014-menu-bar-app.md) | Menu bar app | Native SwiftUI `MenuBarExtra` plus a Settings window in `menubar/`, speaks only the control socket, how it finds it, macOS 14 floor, a Swift fake control server for tests. |
| [0015](0015-integration-results.md) | Integration results | End-to-end tier and benchmark (a script, not a subcommand), memory measured as the physical footprint, the measured table, defects found and fixed, known misses. |
| [0016](0016-verification-results.md) | Verification results | What an independent check of security, robustness, the quickstart, dependencies and repository hygiene found, what changed and which limits stay documented. |
| [0017](0017-homebrew-packaging.md) | Homebrew packaging and the shared install layout | The state directory every install path shares (`bin/voiced` link, `envs/`, `install.json`, identical plists, one lock, the watchdog following a replaced environment); a tap formula built from the lock; why not `brew services` or resource blocks; the menu bar cask as the end-user path. |

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
