# Changelog

All notable changes to voiced. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project
uses [Semantic Versioning](https://semver.org/).

## Unreleased

### Added

- Project skeleton: `src/voiced` package with a `uv` development setup,
  frozen engine interfaces (`STTEngine`, `STTStreamSession`, `TTSEngine`),
  entry-point engine registry, one config file plus `VOICED_*` environment
  overrides, bounded inference queue, `/health`, bearer authentication for
  `/v1`, `voiced serve` and `voiced config`, fake engines for tests, CI on
  `macos-14`.
- Installer and service lifecycle (#3): `voiced install` (service
  environment in `~/.voiced`, model download, staged plists, printed sudo
  script, `--dry-run`), `start`, `stop`, `status`, `logs`, `uninstall`,
  `doctor` with `--metal-probe`, and the `voiced watchdog` daemon that
  restarts hung, stalled or persistently degraded services. New `[watchdog]`
  and `[install]` config keys.
- Shared inference core (#6): every engine call goes through one
  `inference()` block that owns the queue slot, timing, health and metrics;
  one outcome table (`ok`, `busy`, `client_error`, `disconnect`, `failure`)
  for every transport; `run_blocking` so a cancelled request cannot start a
  second GPU call; services bind their ports first and load the model in the
  background; `[server] session_idle_s` and `write_timeout_s`.
- Kokoro text-to-speech engine (#5) with espeak-ng phonemes, text
  normalization (numbers, dates, times, units, acronyms, Markdown) and
  sentence splitting; the `model` pytest marker.
- `SentenceTTSEngine` base class for sentence-at-a-time TTS engines (#9).
- Wyoming listeners for Home Assistant (#1): STT on 10300 and TTS on 10200
  in the same process as the HTTP API, streaming synthesis, bounded STT
  utterances, zeroconf discovery when bound to the network, and the
  `[server] zeroconf` key.
- OpenAI-compatible HTTP API (#2): `/v1/audio/transcriptions` (json, text,
  verbose_json, srt, vtt), the `/v1/audio/transcriptions/stream` WebSocket,
  `/v1/audio/speech` with every format streaming (wav, pcm, mp3, opus),
  `/v1/audio/voices`, the status page at `/` and `/metrics.json`. ffmpeg is
  now a documented dependency.
- Parakeet TDT 0.6B v3 speech-to-text engine (#7), the default: batch
  transcription in overlapping 20 s chunks, streaming with partial
  transcripts and a finalize within milliseconds of the end of speech, and
  Silero VAD through `pysilero-vad` (new core dependency) in `voiced.vad`.
  Known limits: partials trail by about 0.7 s, and a pause of about one
  second can repeat a short word in a streamed transcript.
- Soprano-80M text-to-speech engine (#8), opt-in: one voice, ignores
  `speed`, first audio in about 150 to 220 ms, with a repetition penalty and
  a per-sentence length bound against runaway generation. `scipy` joins the
  `tts` extra for resampling its 32 kHz output.
- Local control channel: the watchdog serves `~/.voiced/control.sock`
  (newline-delimited JSON, 0600, peer uid checked, nothing on the network)
  with status, settings, engine catalogues, restarts and log tails for a
  menu bar app. New commands `voiced restart` and `voiced settings get|set`;
  `voiced status` shows state, uptime and restarts when the watchdog answers,
  and `voiced doctor` checks the socket. Optional settings form on the status
  page behind the new `[server] browser_settings` key (off by default; it
  always needs the API key). Settings writes keep comments (new dependency:
  `tomlkit`).
- Documentation: README with quickstart, architecture diagram, benchmarks
  and troubleshooting; API reference; Home Assistant guide; architecture and
  operations guides; ADR index; contributing guide; issue and pull request
  templates; Dependabot; manual `model-tests` CI job.

### Changed

- CI uses `actions/checkout@v7` and `astral-sh/setup-uv@v10.3.0`, installs
  ffmpeg, and cancels superseded runs of the same branch.
- `parakeet-mlx` is pinned to `>=0.5,<0.6` and `mlx-audio` to
  `>=0.5.8,<0.5.10`, the releases the engines were checked against.
