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
| [0006](0006-install-layout.md) | Install layout | `~/.voiced` for the service environment, staged plists and printed sudo scripts, uninstall safety rules. |
| [0007](0007-http-api.md) | HTTP API layer | ffmpeg as an external dependency, every speech format streaming, upload and input limits, the `model` field, the WebSocket protocol, the status page. |
| [0009](0009-tts-kokoro-espeak-tokens.md) | Kokoro pronounces espeak-ng phonemes | espeak-ng phonemes into the MLX model instead of misaki, how espeak-ng is found, the GPL-3 implication. |
| [0011](0011-wyoming.md) | Wyoming listeners | In-process listeners, STT utterance bounds, per-sentence TTS slots, error codes, zeroconf only when reachable, what Home Assistant expects. |
| [0012](0012-watchdog.md) | Watchdog design | Stdlib-only third daemon, the decision rules, stall detection, restart by SIGKILL plus `kickstart` as a non-root user. Written as a second 0005 and renumbered. |

<!-- TODO(phase2): add 0008 (Parakeet) and 0010 (Soprano) when their PRs merge -->

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
