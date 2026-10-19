# Changelog

All notable changes to voiced. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project
uses [Semantic Versioning](https://semver.org/).

## Unreleased

### Added

- A release workflow: pushing a `v*` tag builds the voiced wheel and sdist
  and the menu bar app as a zip and a DMG with sha256 sums, signs and
  notarizes the app when the Developer ID secrets are set (an ad hoc build,
  named `-adhoc`, otherwise), and creates a draft GitHub release with the
  CHANGELOG section as its notes. The Homebrew formula's `url` and `sha256`
  and a cask for the app come out as an artifact for the tap.
  `menubar/Scripts/sign.sh` does the signing, in CI and by hand; SIGNING.md
  has the one-time setup and CONTRIBUTING.md the steps for cutting a
  release. `bundle.sh` reads the "What's new" notes with the same parser.
- The menu bar app updates, rolls back, repairs and uninstalls an install
  it made (ADR 0018, Lifecycle). Update builds a new environment beside the
  current one and asks for an administrator only when
  `voiced-privileged check` finds the services differ; Roll back returns to
  the previous environment; Repair reinstalls the bundled lock and wheel, or
  builds afresh when the environment's Python is gone; Uninstall removes
  the services, then runs `voiced uninstall --yes`, with the models, logs
  and config as unticked boxes. Homebrew's and the command line's installs
  are shown with the commands that change them.
- `install.json` records `previous`: the environment the active one
  replaced, which pruning keeps and the app rolls back to (null for none).

- The menu bar app's bundle carries what the installer needs (ADR 0018):
  `bundle.sh` adds uv at a pinned version and checksum, the voiced wheel
  built from the checkout with its lock, `install-manifest.json` (Python,
  the wheel's and the lock's sha256, the wheels' download size and the
  pinned models), uv's licence, the third-party `NOTICE` and this
  version's release notes. Each launch checks whether the app runs
  translocated, whether the services run the voiced it carries, and whether
  they are switched off in Login Items. The menu bar icon has idle, busy and
  error states; the menu ends with the app's version; a hidden Debug pane
  lists paths and pins and copies diagnostics; "What's new" shows the notes
  once after an update. A debug build can install a local wheel through the
  `installSource` user default. `bundle.sh` installs the build backend and
  `packaging` with hashes and refuses a checkout with uncommitted changes
  unless given `--dirty`.
- `NOTICE` lists the third-party software the app ships and an install
  downloads, with SPDX licences, generated from
  `src/voiced/requirements.lock` by `scripts/notice.py`; a test keeps the
  two in step. The README gains a third-party section.

- `voiced install --json-progress` (ADR 0018): one JSON event per line on
  stdout (`hello`, `step` start and done per install step, `log`, then
  `done` or `error`, with the exit code agreeing; SIGTERM ends it with
  `error` and stops the running step and what it started); human output
  goes to stderr. `--dry-run --json-progress` lists the steps. The
  watchdog's control-socket `hello` adds what `install.json` records.
- Pinned default models (ADR 0018): `voiced.models.PINS` sets the commit
  and size of Parakeet and Kokoro, the engines load that revision, and
  `voiced install` downloads only the files each engine loads.
  `python -m voiced.models` prints the pins for the app's
  `install-manifest.json`. With `--json-progress` the models step reports
  its bytes as `download` events (at most four a second), and a `models`
  error carries the repo, revision and Hugging Face cache when known.
- A `ports` step in `voiced install`, before the models: each of the four
  configured ports is bound as the services bind it and checked with
  `lsof`, and one another program holds stops the install with the port,
  its config key and its holder (another account's or the system's when
  `lsof` cannot see it). voiced's own
  running services are not a conflict.
- `[tts] espeak_library` and `espeak_data` config keys for the espeak-ng
  that Kokoro loads.
- Homebrew formula (`packaging/homebrew/voiced.rb`, for the
  `JulianBeaulieu/homebrew-voiced` tap): `brew install --HEAD` builds the
  `voiced` command and its environment from the shipped lock, then
  `voiced install` sets up the services with no `--source`. ADR 0017.
- One install layout for every install path (ADR 0017): `~/.voiced` is the
  state directory, `~/.voiced/bin/voiced` a link to the active environment,
  and `install.json` records it. `voiced install` links the environment it
  runs from (Homebrew's keg through `opt`), or with `--source <checkout>`
  builds a fresh one into `~/.voiced/envs/` on a uv-managed Python in
  `~/.voiced/python`, and checks that it imports the engines before linking.
  Switching between Homebrew and a checkout is `voiced install` plus
  `voiced restart all`, with no root step; a `~/.voiced` from before this
  release (a venv) is adopted and replaced on the first switch
  (`voiced install --source <checkout>`). A state directory that is not
  voiced's, a venv of your own included, is refused and never deleted.
  Installs and uninstalls hold a lock on the state directory, and a failed
  install changes nothing.
- Every install path installs `src/voiced/requirements.lock` (`uv export` of
  `uv.lock`, shipped in the package) with `--require-hashes --no-deps`, and
  builds voiced with the hashed build backend in
  `src/voiced/build-requirements.lock`. A test keeps both equal to `uv.lock`.
- The watchdog restarts both services, then itself, when the environment it
  started from is replaced: a keg deleted by `brew upgrade` and cleanup, an
  interpreter deleted by a `python@3.12` upgrade, a keg rebuilt by
  `brew reinstall`, or `~/.voiced/bin/voiced` repointed. It does not follow
  `opt/voiced`, which Homebrew repoints before the new keg is finished, and
  while Homebrew holds the formula's lock it makes no restarts at all (up
  to 30 minutes). The engine catalogue runs that environment's interpreter.
- `voiced doctor` has an Install row (source, environment, version, lock),
  warns when a listening service runs another environment than
  `~/.voiced/bin/voiced` links, and when a daemon is loaded but not
  running.
- The plists' `KeepAlive` holds only while `~/.voiced/bin/voiced` exists, so
  launchd stops respawning the daemons after `brew uninstall voiced`.
  Existing installs pick this up with `voiced install` and the sudo step.
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

- A streamed Wyoming TTS request whose engine failed mid-stream, or whose
  text went over `tts.max_input_chars`, closed the connection right after
  its `Error`. Home Assistant writes the whole stream before it reads, so its
  next write met a reset and the `Error` could be lost. Like a failed STT
  session, the stream now sends the one `Error`, reads and drops the rest of
  the stream, and closes at `SynthesizeStop`, at most
  `server.session_idle_s` after the failure. A refused `SynthesizeStart` now
  has the same deadline.
- A streamed Wyoming STT session that failed (`server_busy` after waiting
  for the slot, the utterance limit, a format change mid-stream, the
  wall-clock bound) closed the connection at once. Home Assistant writes
  every chunk and the `AudioStop` before it reads, so its next write met a
  reset and the `Error` could be lost. The server now sends the one `Error`,
  reads and drops the rest of the utterance, and closes at `AudioStop`, at
  most `server.session_idle_s` after the failure, as it already did for a
  refused TTS stream.
- A streaming STT session that waited for the slot behind another (two
  satellites talking at once) failed with "streaming backlog over 30s" when
  its client had sent more than 30 s meanwhile. `add_audio` now waits for the
  encoder past the backlog bound instead of failing, so the session catches
  up at about six times real time. Sessions still run one at a time: a
  Wyoming session waits about 34 s for the slot before `server_busy`, so
  with three satellites at once the third still fails on utterances over
  about 31 s. ADR 0008 documents the capacity (one stream within the
  finalize target, the rest queue).
- Under launchd the services were slower on short bursts of work than the
  same code started from a terminal: push-to-talk finalize was 145 ms median
  on an M1 Max against 114 ms. A launchd job is not in a terminal app's
  coalition, and macOS runs its first burst of work after an idle gap far
  below full speed. `voiced serve` now sets its own task role to foreground
  application at start, which brings it to 122 ms (ADR 0015).
- `voiced restart all` restarted stt and tts but not the watchdog, which
  kept running the old code after an upgrade until it was killed by hand.
  It now restarts the watchdog last (SIGTERM, then `launchctl kickstart`)
  and waits for the new one to answer on the control socket; `voiced
  restart watchdog` restarts it alone.
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

- ffmpeg comes from the `imageio-ffmpeg` wheel, a core dependency (a static
  ffmpeg 7.1, GPL-2.0-or-later), preferred over one on `PATH`; `brew install
  ffmpeg` is no longer needed. `voiced doctor` names the ffmpeg in use and
  where it comes from.
- voiced ignores a shell's `HF_HUB_CACHE`, `HUGGINGFACE_HUB_CACHE` and
  `HF_ENDPOINT`, like `HF_HOME`: the models come from the hub into the
  cache `[install] hf_home` names.
- The `tts` extra needs `huggingface-hub` 1.1 or later.
- `voiced install` no longer writes plists or `install-daemons.sh` to
  `~/.config/voiced/launchd/`; it removes what earlier releases left there
  and prints one line, `sudo /bin/bash <environment>/.../voiced/launchd/voiced-privileged.sh
  install <user>`, which runs the privileged script the menu bar app runs
  (ADR 0018). `voiced uninstall` prints the same with `uninstall`. The
  `--staging-dir` flag is gone, and the `done` progress event no longer
  carries `staging_dir`. An install with another state directory, config
  path or `[launchd] label_prefix` gets no root step; `label_prefix` now
  only names the labels the CLI and the watchdog address. `voiced doctor
  --metal-probe` writes one script, `~/.config/voiced/metal-probe.sh`, with
  the probe's plist inside it. Existing installs: run the printed line once.
- The LaunchDaemon plists carry only a fixed environment (`HOME`, `PATH`,
  `VOICED_CONFIG`, `PYTHONUNBUFFERED`, `VOICED_STDIO_LOG` for the default
  log directory), the same values the menu bar app's privileged step
  renders. The services take `HF_HOME` (`[install] hf_home`), the espeak-ng
  paths and a custom `[server] log_dir` from the config file at start, and
  these must be absolute (or start with `~`). Every `voiced` command now
  sets `HF_HOME` and `VOICED_ESPEAK_*` from the config alone: values
  exported in your shell are ignored (with a notice for `VOICED_ESPEAK_*`),
  so the services, `doctor` and the installer's model download agree. Re-run `voiced install` and its sudo
  script if you relied on the shell's.
- `voiced install --source` runs uv with `UV_NO_CONFIG=1`, so a `uv.toml`
  of yours cannot change what is installed.
- `voiced install` refuses a state directory holding files that are not
  voiced's, and `voiced uninstall` removes only what the layout owns, keeping
  the rest; before, a directory with `pyvenv.cfg` was deleted whole.
- `voiced install --source` takes a checkout path (no longer a pip
  requirement) and needs uv; `--no-uv` is gone, and `--venv` is now
  `--state-dir` (the old name still works). `[install] python` is a version.
- `voiced doctor` recognises voiced's daemon on a port by its argv[1], the
  entry point.
- CI uses `actions/checkout@v7` and `astral-sh/setup-uv@v10.3.0`, installs
  ffmpeg, and cancels superseded runs of the same branch.
- `parakeet-mlx` is pinned to `>=0.5,<0.6` and `mlx-audio` to
  `>=0.5.8,<0.5.10`, the releases the engines were checked against.
- Uploads that decode to more than about 26 minutes (1562 s) of audio now
  get 413: the decoded audio is bounded by `stt.max_upload_mb` read as
  16-bit 16 kHz samples (1562 s at the default 50). Before, a 30-minute mp3
  was transcribed; now it is refused. Raise `max_upload_mb` to transcribe
  longer recordings (it bounds Wyoming utterances and WebSocket streams the
  same way).

### Security

- Root no longer copies files the user's account can write. The CLI's
  generated `install-daemons.sh` copied plists staged in
  `~/.config/voiced/launchd/` into `/Library/LaunchDaemons` unchecked, so
  any process of the user's could rewrite one after the script was read
  and get a program run as root at boot; its `uninstall-daemons.sh` removed
  another account's daemons. Both are replaced by the menu bar app's
  privileged script, which renders the plists as root from the user name
  alone, refuses system accounts and another account's install, takes a
  lock and calls every tool by path. The CLI prints its line, with the
  path resolved, only when no account but the user's and root can change
  the script or a directory on its path, by mode bits or ACL (not, for
  example, from a Homebrew another admin owns). The
  Metal probe's script carries its plist, uses the fixed label prefix, and
  touches its result file only as the user.
- The LaunchDaemon plists no longer set `StandardOutPath` or
  `StandardErrorPath`. launchd may open those files as root in
  the user-owned log directory, so a symlink planted there could make root
  create or append to any file. The daemons now open
  `<service>.stdout.log` and `.stderr.log` themselves, as the user, refuse
  a symlink, and point their stdout and stderr at them (`VOICED_STDIO_LOG`
  in the plist). Existing installs keep the old plists, and the exposure,
  until the root step `voiced install` prints is run again.
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

