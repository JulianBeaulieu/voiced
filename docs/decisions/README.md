# Architecture decision records

One file per non-obvious choice, numbered in the order they were made.
Status is `accepted` unless the record says otherwise. Add a new record with
the next free number; collisions from parallel branches are renumbered at
merge, and this index is updated when a record lands on `main`.

| ADR | Title | Summary |
|---|---|---|
| [0001](0001-project-structure.md) | Project structure | One package, two services (`stt`, `tts`), shared core modules, MLX engines as optional extras, port assignments. |
| [0002](0002-engine-interfaces.md) | Engine interfaces | Frozen async `STTEngine`, `STTStreamSession` and `TTSEngine` classes, fixed audio formats, entry-point discovery, error and cancellation rules. |
| [0003](0003-launchd-label-prefix.md) | launchd label prefix | `io.github.julianbeaulieu.voiced` as the single-constant prefix, with a planned switch to `computer.voiced`. |

<!-- TODO(phase2): add the records from the merged Phase 1 branches (Wyoming, HTTP API, Kokoro and espeak tokens, launchd daemon vs agent, watchdog, install layout) with their final numbers -->

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
