# Architecture decision records

One file per non-obvious choice, numbered in the order they were made.
Status is `accepted` unless the record says otherwise. Add a new record with
the next free number; collisions from parallel branches are renumbered at
merge.

| ADR | Title | Summary |
|---|---|---|
| [0001](0001-project-structure.md) | Project structure | One package, two processes (`stt`, `tts`), shared core modules, MLX engines as optional extras, port assignments. |
| [0002](0002-engine-interfaces.md) | Engine interfaces | Frozen async `STTEngine`, `STTStreamSession` and `TTSEngine` classes, fixed audio formats, entry-point discovery, error and cancellation rules. |
| [0003](0003-launchd-label-prefix.md) | launchd label prefix | `io.github.julianbeaulieu.voiced` as the single-constant prefix, with a planned switch to `computer.voiced`. |
| 0004 (`0004-wyoming.md`, lands with PR 1) | Wyoming listeners | In-process listeners inside the FastAPI lifespan, one queue slot per STT utterance and per TTS sentence, zeroconf only when reachable, what Home Assistant actually expects. <!-- TODO(phase2): link the file once PR 1 is merged --> |

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
