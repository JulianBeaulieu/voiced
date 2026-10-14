# voiced

Local speech-to-text and text-to-speech server for Apple Silicon. Models run on
the GPU through MLX and stay resident in memory. Exposes an OpenAI-compatible
HTTP API and the Wyoming protocol for Home Assistant, and runs as a launchd
daemon that starts at boot without a GUI login and recovers from crashes and
hangs on its own.

**Status: pre-alpha, under construction.** The interfaces are frozen and the
skeleton runs with fake engines; real inference, the installer and the
Home Assistant integration are being built. Do not deploy yet.

## What it will do

- Speech-to-text with Parakeet TDT 0.6B v3 (`parakeet-mlx`), batch and
  streaming, with silence endpointing.
- Text-to-speech with Kokoro-82M (`mlx-audio`) as the default engine, with
  Soprano-80M installed and selectable.
- `POST /v1/audio/transcriptions` and `POST /v1/audio/speech`, so the official
  OpenAI clients, LiteLLM and Open WebUI work unmodified.
- Wyoming STT on TCP 10300 and TTS on TCP 10200, auto-discovered by Home
  Assistant Assist.
- Two launchd daemons (`stt`, `tts`) plus a watchdog, one-command install, and
  a `doctor` command that checks the machine.
- Pluggable engines: STT and TTS engines are entry-point plugins, so other
  models and languages can be added without forking. Version 1 ships English
  only.

## Quickstart

Not yet. This section will contain the 60-second install once the installer
exists. For development:

```sh
uv sync --group dev
uv run pytest
VOICED_TTS__ENGINE=fake uv run voiced serve tts   # http://127.0.0.1:8880/health
```

## Ports

| Service | HTTP API | Wyoming |
|---|---|---|
| stt | 8881 | 10300 |
| tts | 8880 | 10200 |

All four are configurable. Both services bind to `127.0.0.1` by default;
set `bind = "0.0.0.0"` in the config to serve your LAN, and read the security
note in `config.example.toml` first.

## Configuration

One file, `~/.config/voiced/config.toml`, plus `VOICED_*` environment
variables that override it. Every key has a default. See
[`config.example.toml`](config.example.toml) for the full list.

## Architecture

To be written. Two processes, `stt` and `tts`, each running one FastAPI app
and one Wyoming listener against a single resident model, with a bounded
inference queue in front of the GPU. See [`docs/decisions/`](docs/decisions/)
for the reasoning behind the structure.

## API reference

To be written. Until then, each service serves its OpenAPI schema at `/docs`.

## Home Assistant setup

To be written.

## Benchmarks

To be written. Numbers will be reported for an M1 Max MacBook Pro and an
M1 Max Mac Studio.

## Model licenses

Model weights are not vendored; they are downloaded from Hugging Face on
first run, except Silero VAD, which ships inside the `pysilero-vad` wheel.

| Model | License |
|---|---|
| Parakeet TDT 0.6B v3 | CC-BY-4.0 |
| Kokoro-82M | Apache-2.0 |
| Silero VAD v6.2 via pysilero-vad, bundled | MIT |
| Soprano-80M | to be verified before release |

CC-BY-4.0 requires attribution to NVIDIA when you redistribute the Parakeet
weights.

Kokoro's phonemizer depends on espeak-ng, which is GPL-3. voiced uses it as
an external program (`brew install espeak-ng`) and does not include its
source, so the voiced code itself stays MIT.

## Troubleshooting

To be written.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT. See [LICENSE](LICENSE).
