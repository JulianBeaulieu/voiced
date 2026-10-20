# voiced

[![Release](https://img.shields.io/github/v/release/JulianBeaulieu/voiced)](https://github.com/JulianBeaulieu/voiced/releases/latest)
[![CI](https://github.com/JulianBeaulieu/voiced/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/JulianBeaulieu/voiced/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/github/license/JulianBeaulieu/voiced)](LICENSE)

Run speech-to-text and text-to-speech on your Mac for Home Assistant voice
control or any app that speaks the OpenAI audio API. Models stay loaded on
the Apple Silicon GPU, so with the default voice the first audio of a reply
arrives about 100 ms after the request. voiced runs as two services, one for
each direction, under launchd plus a watchdog: they start at boot without
anyone logging in and restart themselves after a crash or a hang. Home
Assistant talks to it over Wyoming, the protocol its Assist pipelines use
for local speech services.

![A terminal sends a speech request to voiced, transcribes a WAV clip and checks both services' health](docs/demo.gif)

The recording's timings are curl fetching a WAV, slower than the streamed benchmark (96 ms median first audio on the same Apple M1 Max, 64 GB; see [Benchmarks](#benchmarks)).

voiced 0.1.1 is the current release; 0.1.0 was the first. It transcribes
with Parakeet TDT 0.6B v3 and speaks with Kokoro-82M, or with the optional
Soprano-80M or Phonon 31M. The numbers below come from two machines, both
named under [Benchmarks](#benchmarks).
The HTTP and Wyoming API and the config file are stable; see
[CONTRIBUTING.md](CONTRIBUTING.md#what-is-stable). Breaking changes are
listed in [CHANGELOG.md](CHANGELOG.md).

## Install

You need an Apple Silicon Mac with macOS 14 or newer. The first install
downloads about 4.5 GB, mostly models, and takes several minutes.

Most people want the menu bar app, from [Homebrew](https://brew.sh):

```sh
brew install --cask julianbeaulieu/voiced/voiced
```

Open voiced.app. It asks for an administrator password once and installs
the services. The menu bar icon then shows whether both services are up.
The app also restarts them, opens the logs and changes the common settings;
see [menubar/README.md](menubar/README.md). Moving
the app to the Trash leaves the services running: to remove voiced, use
Settings > About > Uninstall voiced in the app.

Using Home Assistant? Continue with
[docs/home-assistant.md](docs/home-assistant.md).

Without Homebrew, download the DMG from the
[release page](https://github.com/JulianBeaulieu/voiced/releases/latest),
drag voiced.app to Applications and open it. Apple has notarized the app,
so macOS only asks once whether to open an app downloaded from the
internet.

For a headless Mac or terminal use, install the formula instead:

```sh
brew install julianbeaulieu/voiced/voiced
voiced install
```

`voiced install` downloads the models and prints one command to run with
`sudo`, the only step that needs root;
[docs/operations.md](docs/operations.md#commands) says what it does. Both
the cask and the formula come from the
[tap](https://github.com/JulianBeaulieu/homebrew-voiced).

Check the services from the terminal. The formula puts `voiced` on your
`PATH`; after an install by the app, the command is `~/.voiced/bin/voiced`.

```sh
voiced status                  # formula
~/.voiced/bin/voiced status    # app
```

Both services report `ok` once their model is warm, a few seconds after the
models are on disk. Try them:

```sh
curl -s http://127.0.0.1:8880/v1/audio/speech \
  -H 'Content-Type: application/json' \
  -d '{"model": "kokoro", "input": "Hello from voiced.", "voice": "af_heart"}' \
  -o hello.wav

curl -s http://127.0.0.1:8881/v1/audio/transcriptions \
  -F file=@hello.wav -F model=parakeet
```

To remove a formula install, run `voiced uninstall`
([docs/operations.md](docs/operations.md#commands)). All three ways of
installing set up the same `~/.voiced` and the same launchd services
([ADR 0017](docs/decisions/0017-homebrew-packaging.md)), so you can switch
between them.

## What you get

| Endpoint | Service | Port | Purpose |
|---|---|---|---|
| `POST /v1/audio/transcriptions` | stt | 8881 | Transcribe a file (json, text, verbose_json, srt, vtt) |
| `WS /v1/audio/transcriptions/stream` | stt | 8881 | Live transcription with partial and final events |
| `POST /v1/audio/speech` | tts | 8880 | Synthesize text as wav, pcm, mp3 or opus; every format streams |
| `GET /v1/audio/voices` | tts | 8880 | List voices |
| `GET /v1/models` | both | | The model this service runs, its languages and license |
| `GET /health` | both | | Model loaded, queue depth, last inference |
| `GET /` and `GET /metrics.json` | both | | Status page and its data |
| Wyoming STT | stt | 10300 | Home Assistant speech-to-text |
| Wyoming TTS | tts | 10200 | Home Assistant text-to-speech, streaming |

Every port is configurable. The full reference, including the WebSocket
messages, the error shapes and the Wyoming behaviour, is in
[docs/api.md](docs/api.md). The Home Assistant walkthrough is in
[docs/home-assistant.md](docs/home-assistant.md).

Works unmodified with the official `openai` clients, LiteLLM, Open WebUI and
anything else that speaks the OpenAI audio API. With `pip install openai`
(or `uv run --with openai python`):

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8880/v1", api_key="unused")
audio = client.audio.speech.create(model="kokoro", voice="af_heart", input="Hello.")
audio.write_to_file("hello.wav")
```

## Architecture

```mermaid
flowchart LR
    subgraph clients [Clients]
        oai[OpenAI clients]
        ha[Home Assistant]
    end

    subgraph stt [stt service]
        stt_http[HTTP API :8881]
        stt_wy[Wyoming :10300]
        stt_q[Inference queue]
        parakeet[Parakeet TDT 0.6B v3]
        stt_http --> stt_q
        stt_wy --> stt_q
        stt_q --> parakeet
    end

    subgraph tts [tts service]
        tts_http[HTTP API :8880]
        tts_wy[Wyoming :10200]
        tts_q[Inference queue]
        kokoro[Kokoro-82M]
        tts_http --> tts_q
        tts_wy --> tts_q
        tts_q --> kokoro
    end

    oai --> stt_http
    oai --> tts_http
    ha --> stt_wy
    ha --> tts_wy

    launchd[launchd] -->|KeepAlive| stt
    launchd -->|KeepAlive| tts
    watchdog[watchdog] -->|GET /health| stt_http
    watchdog -->|GET /health| tts_http
    watchdog -.->|SIGKILL, kickstart| launchd
```

Two services, one package. Each service is one process that loads one
model, serves it over HTTP and Wyoming from the same event loop, and
serializes GPU work through a bounded queue. launchd restarts a service that
exits; the watchdog restarts one that is alive but stuck. Engines are
entry-point plugins behind a
[stable interface](CONTRIBUTING.md#what-is-stable). Details, including the audio
conventions and how to add an engine, are in
[docs/architecture.md](docs/architecture.md).

## Configuration

One file, `~/.config/voiced/config.toml`, plus environment variables that
override it. `voiced install` creates the file from
[`config.example.toml`](config.example.toml), which lists every key with its
default and a comment. Every key has a default, so the services also start
with no file at all.

Environment variables are named `VOICED_<SECTION>__<KEY>`:

```sh
VOICED_SERVER__BIND=0.0.0.0
VOICED_TTS__DEFAULT_VOICE=bf_emma
VOICED_STT__LANGUAGES='["en"]'    # lists are JSON
VOICED_CONFIG=/path/to/other.toml # use a different config file
```

`voiced config` prints the effective configuration as JSON. Unknown keys are
errors, so a typo stops the service with a message instead of being ignored.

### Security note on `bind`

Both services bind to `127.0.0.1` by default and are reachable only from the
Mac itself. Home Assistant on another machine needs `bind = "0.0.0.0"`, which
exposes the services to your whole network:

- The HTTP API has no authentication unless `api_key` is set. Set it before
  you bind to the LAN. `/health`, `/`, `/metrics.json` and `/docs` stay open
  either way. The first three show latencies, error counts and recent error messages; with
  `api_key` set the error messages are left out, since they can echo what a
  client sent. Without a key, a web page you open can also send requests to
  the services on your own Mac.
- The Wyoming protocol has no authentication at all. Only expose it on a
  network you trust. Anyone who can reach the ports can use the models and
  keep them busy; the listeners bound what one client can make them buffer
  or hold open, but they cannot tell Home Assistant from anyone else. The
  same is true of the HTTP API without a key.
- The config file is created readable only by you (0600), because the API
  key goes there. Other accounts on the Mac can read files in your home
  directory that are not, so keep it that way if you edit it by hand.
- voiced does no TLS. Put a reverse proxy in front if you need HTTPS.
- Settings and restarts go through a Unix socket that only your user can
  open (`voiced settings`, `voiced restart`). The status page's settings
  form is off unless `browser_settings = true`, and even then works only
  with an API key set.

## Models and licenses

Weights are not vendored. Each engine downloads its model from Hugging Face
into the local cache, during `voiced install` or on first start. Silero VAD
is the exception: it ships inside the `pysilero-vad` wheel.

| Model | Role | Default | License |
|---|---|---|---|
| [Parakeet TDT 0.6B v3](https://huggingface.co/mlx-community/parakeet-tdt-0.6b-v3) | STT | yes | CC-BY-4.0 |
| [Kokoro-82M](https://huggingface.co/mlx-community/Kokoro-82M-bf16) | TTS | yes | Apache-2.0 |
| [Soprano-80M](https://huggingface.co/mlx-community/Soprano-80M-bf16) | TTS | no, opt-in | Apache-2.0 |
| [Phonon 31M](https://huggingface.co/gradium/phonon-31m-en) and its [voices](https://huggingface.co/gradium/phonon-voices) | TTS | no, opt-in | Apache-2.0 |
| [Phonon 40M](https://huggingface.co/gradium/phonon-40m-en), English and [multilingual](https://huggingface.co/gradium/phonon-40m-multilingual) | TTS | no, opt-in, gated | CC-BY-NC-4.0 |
| Silero VAD v6.2 via `pysilero-vad`, bundled | speech detection | yes | MIT |

CC-BY-4.0 requires attribution to NVIDIA when you redistribute the Parakeet
weights. It says nothing about the transcripts you produce.

Phonon is Gradium's. It builds on Kyutai's Pocket TTS and Mimi codec, both
released under CC-BY-4.0; Gradium releases Phonon 31M's weights, tokenizer
and voices under Apache-2.0, and its runtime, the `ptts` package, under MIT
or Apache-2.0. The Phonon 40M models are CC-BY-NC-4.0: non-commercial use
only. They are gated on Hugging Face, so using one means accepting its terms
on the model page and giving the services a token (see
[Using Phonon](#using-phonon)). voiced never uses them unless the config
names one, and `/v1/models` reports the licence of the model in use.

Kokoro reads phonemes produced by espeak-ng, which is GPL-3. voiced does not
vendor or redistribute it, and the voiced code stays MIT. The engine loads
espeak-ng at run time as a shared library. The `tts` extra pulls in, as
dependencies of `kokoro-onnx`, the `phonemizer` package (GPL-3.0-or-later),
which the Kokoro engine imports, through `kokoro_onnx.tokenizer`, to drive
espeak-ng in the tts process, and the `espeakng-loader` wheel, which carries
its own GPL-3 copy of espeak-ng. voiced loads that wheel's copy, so every
install runs the version in the lock and needs nothing from Homebrew; if it
does not work, Homebrew's (`brew install espeak-ng`) is tried next, and the
path in `[tts] espeak_library` replaces both when set. The engine fails at
startup with the remedies if none works. With `[tts] espeak_library` set
and `espeak_data` unset, the wheel's espeak-ng data files are used. Anyone
redistributing a tts environment redistributes these GPL-3 packages with
it: shipping a built tts environment (a venv, an app bundle, an image)
means meeting the GPL-3 terms for it.

Libraries: `zeroconf` is LGPL-2.1-or-later and is imported as an installed
package, not vendored; `soxr` and the `libsndfile` build inside `soundfile`
(both pulled in by `librosa`, a `parakeet-mlx` dependency) are
LGPL-2.1-or-later; `certifi` and parts of `tqdm` are MPL-2.0; `parakeet-mlx`
is Apache-2.0; `mlx-audio`, `wyoming`, `pysilero-vad` and `kokoro-onnx` are
MIT. ffmpeg runs as a separate program: the static ffmpeg 7.1 inside the
`imageio-ffmpeg` wheel (the package BSD-2-Clause, the binary
GPL-2.0-or-later), installed from PyPI like the rest and preferred over any
ffmpeg on `PATH`, so every Mac runs the build voiced was tested with
(`voiced doctor` names the one in use).

### Third-party software

[NOTICE](NOTICE) lists every third-party component with its SPDX licence and
a link to its source: the Python packages in `src/voiced/requirements.lock`,
the CPython that installs use, uv, and the native code some wheels carry
(espeak-ng in `espeakng-loader`, GPL-3.0-or-later; the ffmpeg build in
`imageio-ffmpeg`, GPL-2.0-or-later). `scripts/notice.py`
writes it from the lock and a test keeps the two in step. The menu bar app
shows the same file under Settings > About.

The menu bar app carries only voiced's own MIT code and uv (MIT or
Apache-2.0). The GPL and LGPL components (phonemizer, espeak-ng, ffmpeg,
soxr, zeroconf and the rest NOTICE names) are downloaded from PyPI by your
own Mac when voiced is installed. voiced never distributes them: they are
not in the app, the cask or any release asset (ADR 0018, "Licences").

### Using Soprano

Soprano-80M is installed with the `tts` extra but is opt-in. To use it, set
all three keys under `[tts]` in `~/.config/voiced/config.toml`:

```toml
[tts]
engine = "soprano"
model = "mlx-community/Soprano-80M-bf16"
default_voice = "default"
```

Then run `~/.voiced/bin/voiced install`, which downloads the model (from
the service environment, so nothing is rebuilt), and `voiced restart all`.
Know its limits before you switch. Its first audio takes about 150 to
220 ms, against Kokoro's 106 ms. It has no rate control, so `speed` is
accepted and ignored. It has one voice, `default`. It sometimes runs on past
the end of a sentence; voiced cuts each sentence at a length bound with a
short fade and logs `soprano: runaway generation`, which can clip the last
word, most often on one- and two-word replies. The details are in
[ADR 0010](docs/decisions/0010-tts-soprano.md).

### Using Phonon

Phonon is a small text-to-speech model that runs on the CPU, so it leaves
the GPU to speech-to-text. It is installed with voiced but is opt-in. To use
it, set all three keys under `[tts]`:

```toml
[tts]
engine = "phonon"
model = "gradium/phonon-31m-en"
default_voice = "default"
```

`default` is the model's own voice. The other 37 are Gradium's English
presets, named as Gradium names them (`Easton`, `Maeve`, `The Snow Queen`);
`/v1/audio/voices` and the menu bar app list them. Then run
`~/.voiced/bin/voiced install`, which downloads the model and the voices
(about 75 MB), and `voiced restart all`. Like Soprano, it has no rate
control, so `speed` is accepted and ignored. Voice cloning needs Gradium's
hosted service and is not supported.

The larger `gradium/phonon-40m-en` and `gradium/phonon-40m-multilingual`
work the same way, as `model`, under their non-commercial licence; the
other Phonon 40M models do not speak English and are refused. They are
gated: accept the terms on the model's Hugging Face page while signed in,
then, as the user the services run as, run `hf auth login` with `HF_HOME`
set to the Hugging Face home the services use (`[install] hf_home`, or
`~/.cache/huggingface` when unset), so the token lands where they read it.
The details are in [ADR 0021](docs/decisions/0021-tts-phonon.md).

## Benchmarks

Apple M1 Max with 64 GB, macOS 15.7, warm model, local request, each engine
alone in its process. The end-to-end table further down adds a Mac Studio.
"First audio" is the time to the first PCM chunk.

| Metric | Measured | Source |
|---|---|---|
| Kokoro first audio, multi-sentence text | 106 ms median | Kokoro engine, 2026-10-14 |
| Kokoro real-time factor | 0.046 | Kokoro engine, 2026-10-14 |
| Kokoro load including warm-up | 1.9 s | Kokoro engine, 2026-10-14 |
| Kokoro memory: maximum RSS, MLX active | 469 MB, 326 MB | Kokoro engine, 2026-10-14 |
| Soprano first audio | 156 to 223 ms median | Soprano engine, 2026-10-14 |
| Soprano resident memory | 392 MB | Soprano engine, 2026-10-14 |
| Parakeet batch, 9.4 s clip | 163 ms | Parakeet engine, 2026-10-14 |
| Parakeet streaming finalize after the 200 ms silence window | about 1 ms median, under 150 ms worst | Parakeet engine, 2026-10-14 |
| Parakeet first partial after the first word | about 0.7 s | Parakeet engine, 2026-10-14 |
| Parakeet load including warm-up, warm disk cache | 1.2 s | Parakeet engine, 2026-10-14 |
| Parakeet memory: after load, serving peak, load peak | 1.28 GB, 2.46 GB, 3.6 to 4.1 GB | Parakeet engine, 2026-10-14 |

Parakeet's figures are MLX allocations; its load peak happens once, while
the fp32 weights are cast to bf16, and is over the 3 GB the two services aim
to stay under together while serving. Known limits of streaming
transcription: partial transcripts trail the speaker by about 0.7 s, and a
pause of about one second inside an utterance can repeat a short word ("set
the the thermostat").
Batch transcription is not affected. The measurements are in
[ADR 0008](docs/decisions/0008-stt-engine-parakeet.md).

### Measured end-to-end

The assembled server measured as one system with `scripts/bench.py`, the
`openai` client and the WebSocket over real sockets, each service in its own
process. Method, the complete table and the known misses are in
[ADR 0015](docs/decisions/0015-integration-results.md).

- First audio: from the request to the first PCM byte, streamed through the
  `openai` client, for the sentence "Turn on the kitchen lights."
- Finalize: from the client's `end` message to the `final` event, after 200
  ms of trailing digital silence (the endpointer's window). A push-to-talk
  client that sends `end` at the last word sees about 114 ms median instead,
  worst up to 165 ms and 192 ms while TTS synthesizes (on the Studio 146 to
  149 ms median, 170 ms worst, and 510 ms worst while TTS synthesizes); that
  misses the project's 50 ms median target
  ([ADR 0008](docs/decisions/0008-stt-engine-parakeet.md)). Those TTS figures
  are Kokoro's; while Phonon synthesizes, on the M1 Max, the worst was
  115 ms ([ADR 0021](docs/decisions/0021-tts-phonon.md)).
- Memory: the physical footprint (what Activity Monitor counts, GPU buffers
  included), idle after a long reply, with the MLX peak reached while serving.

| Metric | Apple M1 Max, 64 GB (macOS 15.7.4) | Mac Studio, M1 Max, 32 GB (macOS 27.0.1) |
|---|---|---|
| Kokoro first audio | 96 ms median, 112 ms worst | 85 ms median, 89 ms worst |
| Streaming finalize, 200 ms trailing silence, 32 alignments each at 64 and 100 ms chunks | 1 ms median, 16 ms worst | 3 ms median, 59 ms worst |
| Batch transcription, 10.1 s clip | 253 ms median | 258 ms median |
| STT + TTS footprint, idle after use | 2.21 GB; over 3 GB for a moment during a long reply (Kokoro MLX peak 2.16 GB) | 2.36 GB (Kokoro MLX peak 2.16 GB) |
| Phonon 31M first audio (opt-in, on the CPU) | 28 ms median, 33 ms worst; 19 ms median, 28 ms worst while STT streams | not measured |
| Phonon 31M footprint, idle after use | 0.26 GB | not measured |
| End of speech to first audio, STT -> LLM -> TTS | not measured | 436 ms median (STT 7 + LLM 217 + TTS 221), `ministral-3:8b` on Ollama, from a laptop over Wi-Fi |

The Studio ran the services as LaunchDaemons with Ollama's `ministral-3:8b`
resident on the same GPU. Its rows are measured on the Studio over loopback,
except the loop row. Over Wi-Fi from a laptop the batch row is about 70 ms
slower (326 ms median, the upload) and first audio about 20 ms slower (105 ms
median). On the Studio, finalize is much slower while Kokoro synthesizes
(179 ms median, 250 ms worst with 200 ms trailing silence); see the ADR.

## Troubleshooting

Start with `voiced doctor`. It checks Apple Silicon, the macOS version,
Python, espeak-ng, ffmpeg, memory, port conflicts, FileVault, the installed
daemons and Metal access, including from a real daemon context with
`voiced doctor --metal-probe`.

| Symptom | Likely cause and fix |
|---|---|
| `/health` returns 503 with `"status": "starting"`, `/v1` returns 503 `server_busy` | The model is still loading. The first start downloads it and can take minutes; watch `voiced logs`. |
| `/health` says `"degraded"` | The last inference failed. The error is in `last_error` and on the status page; the watchdog restarts the service if it stays degraded for two minutes. |
| HTTP 503 `server_busy` while the model is loaded | The inference queue is full. Raise `queue_depth` or send fewer concurrent requests. |
| HTTP 401 `auth_error` | `api_key` is set; send `Authorization: Bearer <key>`. |
| tts exits at startup with `EspeakNotFound` | Neither the espeak-ng in voiced's environment nor Homebrew's works; the message names each copy tried. Run `voiced doctor`, reinstall voiced, or `brew install espeak-ng` (if Homebrew's old `espeak` formula is installed, `brew unlink espeak` first). |
| HTTP 500 `ffmpeg_missing` | Uploads and mp3 or opus output need ffmpeg, which comes with voiced's `imageio-ffmpeg` dependency: reinstall voiced. |
| HTTP 400 `invalid_audio` | ffmpeg could not decode the upload. |
| Soprano clips the last word; the log shows `soprano: runaway generation` | The model ran past the sentence and was cut at its length bound. Expected now and then, mostly on one- and two-word replies; Kokoro does not do this. |
| tts exits at startup with `gradium/phonon-40m-... is gated` | The configured Phonon 40M model needs its licence accepted and a Hugging Face token where the services read it; the message says how. See [Using Phonon](#using-phonon). |
| Streamed transcript repeats a short word after a pause | A known limit of streaming with Parakeet around one-second pauses; batch transcription of the same audio is not affected. |
| A service exits at startup with `unknown stt engine` or `unknown tts engine` | `engine` in the config names an engine that is not installed; the message lists the installed ones. |
| ERROR log line `ASGI callable returned without completing response` | Expected; see [Logs](docs/operations.md#logs). |
| `voiced stop` prints a `sudo` command | Unloading a LaunchDaemon needs root; run the printed command. |
| Home Assistant does not discover voiced | `bind` is still `127.0.0.1`, or mDNS does not cross your VLAN. See [docs/home-assistant.md](docs/home-assistant.md). |
| Services do not come up after a reboot | FileVault waits for a disk unlock; see [docs/operations.md](docs/operations.md#filevault). |
| Port already in use | Another program owns 8880, 8881, 10200 or 10300. `voiced doctor` names it; change the port in the config. |
| Services stopped after `brew uninstall voiced` | `~/.voiced/bin/voiced` points at the removed formula, so launchd no longer starts them. Reinstall it, or finish removing voiced: [docs/operations.md](docs/operations.md#homebrew). |
| A service restarts every 10 s after a reboot, log shows `LocalEntryNotFoundError` | The models are not in the Hugging Face cache and there is no network. It recovers by itself once the download can happen. With the models cached, a service starts without network. |

Logs live under `~/Library/Logs/voiced/`. More in
[docs/operations.md](docs/operations.md).

## Roadmap

Version 1 is English only with Parakeet and Kokoro. More languages and more
engines (Whisper, Piper, Pocket TTS and others) are plugins, not forks; see
[Adding an engine](docs/architecture.md#adding-an-engine). Requests go in the
[issue tracker](https://github.com/JulianBeaulieu/voiced/issues).

## Contributing

Contributions are welcome. [CONTRIBUTING.md](CONTRIBUTING.md) has the setup,
the checks and how a change gets merged.

- Bugs and feature requests go in
  [issues](https://github.com/JulianBeaulieu/voiced/issues). Questions go in
  [Discussions](https://github.com/JulianBeaulieu/voiced/discussions).
- Small fixes can go straight to a pull request. Start larger changes with an
  issue, so the approach is agreed before the work.
- A new model or language is an engine, and engines are plugins: see
  [Adding an engine](docs/architecture.md#adding-an-engine).
- To run `main` from a checkout, see
  [Installing from a checkout](docs/operations.md#installing-from-a-checkout).

Everyone taking part follows the [code of conduct](CODE_OF_CONDUCT.md).

## Security

Report vulnerabilities privately, not in a public issue.
[SECURITY.md](SECURITY.md) says how and what is in scope.

## Documentation

| Document | Contents |
|---|---|
| [docs/api.md](docs/api.md) | HTTP, WebSocket and Wyoming reference with curl and Python examples |
| [docs/home-assistant.md](docs/home-assistant.md) | Wyoming setup, Assist pipeline, optional Ollama conversation agent |
| [docs/architecture.md](docs/architecture.md) | Services, inference path, queue, engine registry, audio conventions, adding an engine |
| [docs/operations.md](docs/operations.md) | Commands, installing from a checkout, launchd, watchdog, control channel, logs, FileVault, memory, updating |
| [docs/decisions/](docs/decisions/README.md) | Architecture decision records |
| [menubar/README.md](menubar/README.md) | The macOS menu bar app: building, running it against the fake control server |
| [CONTRIBUTING.md](CONTRIBUTING.md) | How to contribute, development setup, tests, conventions |
| [SECURITY.md](SECURITY.md) | Supported versions, reporting a vulnerability, threat model |
| [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) | The Contributor Covenant, and how to report a problem |
| [CHANGELOG.md](CHANGELOG.md) | Release notes |

## License

MIT. See [LICENSE](LICENSE). Model weights, espeak-ng, ffmpeg and the
libraries above have their own licenses, listed under
[Models and licenses](#models-and-licenses).
