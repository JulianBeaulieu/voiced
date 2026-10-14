# 0001: Project structure

Date: 2026-10-14. Status: accepted.

## Context

voiced serves two modalities (speech-to-text and text-to-speech) that need to
fail independently, share a lot of plumbing, and be installable by people who
are not the author. It must also be extensible with other models without
forking.

## Decision

One repository and one Python package, `voiced`, with a `src/` layout and
`uv` for dependency management. Hatchling is the build backend.

Two service processes, `stt` and `tts`, each built from the same core:

| Module | Role |
|---|---|
| `voiced.config` | One `Settings` model; TOML file plus `VOICED_*` env overrides. |
| `voiced.logging` | Stderr plus rotating file under `~/Library/Logs/voiced/`. |
| `voiced.health` | `HealthState` and the frozen `/health` schema. |
| `voiced.queue` | `InferenceQueue`: serialized, bounded, 503 on overflow. |
| `voiced.engines` | Frozen engine interfaces and the entry-point registry. |
| `voiced.api` | FastAPI app factories, one per modality. |
| `voiced.wyoming` | Wyoming listeners, run in-process with the HTTP app. |
| `voiced.stt`, `voiced.tts` | Wire a configured engine into an app. |
| `voiced.launchd` | Plist rendering. |
| `voiced.cli` | `voiced install | start | stop | status | logs | uninstall | doctor | serve | config`. |
| `voiced.testing` | Fake engines so everything above is testable without models. |

Dependencies are split so CI and non-Apple machines can install the core:
the MLX engines are optional extras (`stt`, `tts`), the tools are a `dev`
dependency group.

Ports: the TTS HTTP API takes 8880 because the prototype and other
Kokoro-serving projects use it; STT takes the next one, 8881. Wyoming uses the
conventional 10300 (STT) and 10200 (TTS). All four are configurable.

Python: `>=3.11,<3.14`, pinned to 3.12 for development. The upper bound comes
from the MLX ecosystem (`kokoro-onnx` declares `<3.14`).

## Consequences

- The two processes cannot share one loaded model, which is fine because they
  load different ones.
- Anything that both processes need lives in the top-level modules, not in
  `stt/` or `tts/`.
- Every piece of the server is exercised in CI against the fakes on a GitHub
  `macos-14` runner; model-dependent tests are a separate, manual tier.
