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
| `voiced.context` | `ServiceContext`: engine, queue, health, metrics and settings for one service, shared by all transports. |
| `voiced.inference` | `inference()` around every engine call: slot, timing, health, metrics; `classify()` and `bounded()` for transports (ADR 0005). |
| `voiced.threads` | `run_blocking()`: thread calls that keep the slot until the GPU is idle, even on cancel; `ModelThread`: the same, pinned to one thread per MLX engine (MLX streams are per thread). |
| `voiced.metrics` | `Operation`, `Outcome`; per-operation p50/p95 latency, outcome counts, recent errors, resident memory. |
| `voiced.audio` | PCM conversion (`PCMConverter`, int16 and float32), `resample` (polyphase, for engines whose model rate is not 24 kHz), WAV headers, `frame_rms` / `dbfs_to_amplitude` on the shared 32 ms frame, and the STT input checks both STT engines share (`check_samples`, `is_silent` against `SILENCE_FLOOR`). No transport imports. |
| `voiced.vad` | Endpointing: `Endpointer` (speech start and end with the `vad_silence_ms` window) over `SileroDetector`. Top level because the Parakeet streaming planner uses it; the transports end a turn on the client's end-of-stream signal (the WebSocket `end` message or Wyoming `AudioStop`), not a server-side endpointer. |
| `voiced.engines` | Frozen engine interfaces and the entry-point registry. |
| `voiced.engines.sentence` | `SentenceTTSEngine`: `load()`, the loaded, voice and speed checks, and the split-synthesize-yield loop shared by sentence-at-a-time TTS engines. |
| `voiced.engines.kokoro` | The Kokoro TTS engine (espeak-ng phonemes into MLX); see ADR 0009. |
| `voiced.engines.soprano` | The Soprano TTS engine, selectable, not default (repetition penalty and a per-sentence bound against runaways); see ADR 0010. |
| `voiced.engines.phonon` | The Phonon TTS engine on the CPU through `ptts`, selectable, not default (chunks streamed within a sentence, pinned model and voices); see ADR 0021. |
| `voiced.engines.moonshine` | The Moonshine Streaming STT engine on the CPU through `moonshine-voice`, selectable, not default (the runtime's stream, finalized on the client's end signal); see ADR 0026. |
| `voiced.engines.omnivoice` | The OmniVoice TTS engine on MLX, selectable, not default (646 languages, designed voices, no streaming within a sentence); see ADR 0030. |
| `voiced.engines.parakeet` | The Parakeet TDT STT engine (`parakeet-mlx`): batch, and streaming through a planner (gate, grid, speculative final call) and a thin library seam; see ADR 0008. |
| `voiced.engines.qwen3_asr` | The Qwen3-ASR STT engine through mlx-audio, selectable, not default (batch with forced-aligner segments, BCP-47 language mapping, a stream buffered and transcribed at finalize); see ADR 0027. |
| `voiced.text` | Text normalization and sentence splitting; pure Python, imported by engines and both API layers. |
| `voiced.api` | FastAPI app factories, one per modality; `api.context` holds `Ctx`, `APIError` and the `/v1` dependencies; `api.ffmpeg` decodes uploads and encodes mp3/opus through ffmpeg; `api.formats` renders transcripts (json, text, verbose_json, srt, vtt); `api.foreign_models` rejects a `model` that names another installed engine; `api.responses` holds `ClosingStreamingResponse`; `api.status_page` serves `/` and `/metrics.json`. |
| `voiced.wyoming` | Wyoming listeners, run in-process with the HTTP app; see ADR 0011. |
| `voiced.stt`, `voiced.tts` | Wire a configured engine into an app. |
| `voiced.launchd` | `plist` rendering, `control` (launchctl argv builders and runners), `install` (the installer plan, scripts, uninstall safety), `metal_probe`. |
| `voiced.watchdog` | Stdlib-only hang watchdog: health polling, decision rules, restart. |
| `voiced.doctor` | Machine checks for `voiced doctor`, each an injectable function. |
| `voiced.table` | Plain-text table rendering shared by `status` and `doctor`. |
| `voiced.cli` | Command tree and dispatch; `serve` and `config` live here. |
| `voiced.cli_lifecycle` | `install`, `start`, `stop`, `status`, `logs`, `uninstall`, `doctor`, `watchdog`. |
| `voiced.testing` | Fake engines so everything above is testable without models, and `EnergyDetector`, a deterministic speech detector for endpointing tests. |

Dependencies are split so CI and non-Apple machines can install the core:
the MLX engines are optional extras (`stt`, `tts`), the tools are a `dev`
dependency group.

Ports: the TTS HTTP API takes 8880 because other Kokoro-serving
projects use it; STT takes the next one, 8881. Wyoming uses the
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
