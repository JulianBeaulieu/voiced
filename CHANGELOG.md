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
- End-to-end test tier (`tests/e2e/`, model tier) that runs the real
  services with real engines and drives them with the `openai` client, the
  WebSocket stream and the Wyoming client, and `scripts/bench.py`, which
  measures first audio, streaming finalize, batch transcription, memory and
  the speech-to-LLM-to-speech loop against live servers (ADR 0015).
- `/metrics.json` reports `memory.footprint_bytes` (the physical footprint,
  which includes MLX's GPU buffers) and `memory.mlx` (active, cached and peak
  allocation); the status page shows the footprint.
- Documentation: README with quickstart, architecture diagram, benchmarks
  and troubleshooting; API reference; Home Assistant guide; architecture and
  operations guides; ADR index; contributing guide; issue and pull request
  templates; Dependabot; manual `model-tests` CI job.

### Fixed

- `voiced install` built an invalid pip requirement (`voiced==X[stt,tts]`)
  for a pinned release; extras now go before the version.
- The TTS engines never bounded MLX's buffer cache, so the Kokoro service
  grew to several GB after a few replies; the cache is now capped at 256 MB
  and emptied after the warm-up.
- `voiced serve` with a port already in use exited 0, or only after loading
  the model; it now names the port and exits 3 before loading anything.
- The `finalize` contract said the server's endpointer ends a streaming turn;
  the client's `end` message or Wyoming `AudioStop` does.
- `voiced doctor` reports the service environment's Python, including a
  broken link into a removed uv Python, warns when the config file is
  readable by other accounts, and checks that ffmpeg can read from a file
  descriptor. `voiced status` says how to install the daemons when none
  are installed.
- The engine catalogue child process no longer imports modules from the
  watchdog's working directory.
- Wyoming failures are now logged, with a traceback, like HTTP ones.
- README: the quickstart pins `--python 3.12` for `uv tool install`, gives
  measured download sizes, and lists the GPL-3, LGPL and MPL packages the
  extras pull in.

### Changed

- CI uses `actions/checkout@v7` and `astral-sh/setup-uv@v10.3.0`, installs
  ffmpeg, and cancels superseded runs of the same branch.
- `parakeet-mlx` is pinned to `>=0.5,<0.6` and `mlx-audio` to
  `>=0.5.8,<0.5.10`, the releases the engines were checked against.
- An uploaded file may now decode to at most `stt.max_upload_mb` read as
  16-bit 16 kHz audio, about 26 minutes at the default 50: a 30 MB mp3 of a
  40-minute meeting now gets 413. Raise `max_upload_mb` to transcribe
  longer recordings (it bounds Wyoming utterances and WebSocket streams the
  same way).

### Security

- Uploads can no longer make ffmpeg open other files: ffmpeg reads the
  upload through an inherited descriptor and may open nothing else, so an
  `ffconcat` list or playlist is refused. Upload files are anonymous and
  never left behind. The decoded audio is bounded like a Wyoming utterance
  (413), and at most `queue_depth` uploads decode at once (503). ffmpeg must
  support its `fd` protocol; `voiced doctor` checks.
- Wyoming listeners refuse an oversize event from its header instead of
  buffering it, close connections that stay silent for five minutes or stop
  halfway through an event, and take at most 64 connections each. Malformed
  events and unsupported audio formats are client errors instead of failures
  that marked the service degraded. Voices are checked against the served
  list, as on HTTP.
- The WebSocket stream has the same audio and wall-clock bound as a Wyoming
  utterance.
- With `api_key` set, `/health` and `/metrics.json` show only the exception
  name of the last error.
- `voiced install` refuses to run as root, and its `--user` option is gone:
  the daemons run the installing user's venv, so they must run as that
  user, never as root or anyone else. That user comes from the process's
  uid, not from `LOGNAME` or `USER`. Outside a checkout it
  requires `--source` until voiced is published. The config file is created
  0600, and re-running `voiced install` tightens an existing one: after
  upgrading, run it once (or `chmod 600 ~/.config/voiced/config.toml`).
- `voiced serve` raises its open file limit from launchd's 256 to 4096.

