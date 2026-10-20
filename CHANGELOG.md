# Changelog

All notable changes to voiced. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project
uses [Semantic Versioning](https://semver.org/).

## Unreleased

## [0.1.1] - 2026-10-20

### Fixed

- Installs from the cask or the DMG no longer need Homebrew's espeak-ng.
  On a Mac without it, which was every Mac set up only with the menu bar
  app, 0.1.0's text-to-speech failed to start with `EspeakNotFound` ("no
  espeak-ng library found"). voiced now uses the espeak-ng that comes with
  its own environment, and Homebrew's only if that one does not work.
  `voiced doctor` names the copy it will use.
- `GET /openapi.json` on the TTS service no longer returns 500, so `/docs`
  shows the API.
- Two installs within the same second, by `voiced install --source` or the
  menu bar app, no longer get the same environment name, which could leave
  the rollback target in `install.json` pointing at the wrong environment
  or build into the environment in use.

### Changed

- The README leads with what voiced is and how to install it.
  CONTRIBUTING.md explains how to contribute and what is stable, and the
  repository has a security policy (SECURITY.md), a code of conduct and
  issue forms that ask how voiced was installed.

## [0.1.0] - 2026-10-19

### Added

- Several live speech-to-text streams are transcribed at once: up to
  `stt.streams` (default 2) WebSocket and Wyoming sessions share the
  service, so two satellites talking together do not queue behind each
  other (ADR 0020). Each session has its own stream state over the one
  loaded model (36 MB each; the weights are shared), and its encoder calls
  take turns on the engine's one model thread. The calls a client waits on
  go first, then sessions that are behind, then the rest. With two clients
  at once, finalize after the endpointer's 200 ms window was worst 69 ms on
  an M1 Max and 103 to 110 ms on the Mac Studio. `stt.streams = 3` serves
  a third at once; its finalize stays within 150 ms at the 95th percentile,
  but rarely one waits for both other sessions' calls and takes 162 to
  186 ms. A session past `stt.streams` waits for one to end, and a batch
  transcription waits until no stream is open. `stt.streams` above
  `queue_depth` is a configuration error. An STT engine declares how
  many sessions it can interleave (`STTEngine.max_streams`, 1 by default,
  6 for Parakeet), and the service never opens more. `scripts/bench.py`
  has a row for `--streams` clients at once.
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
  two in step. The README has a third-party section.
- `voiced install --json-progress` (ADR 0018): one JSON event per line on
  stdout (`hello`, `step` start and done per install step, `log`, then
  `done` or `error`, with the exit code agreeing; SIGTERM ends it with
  `error` and stops the running step and what it started); human output
  goes to stderr. `--dry-run --json-progress` lists the steps. The
  watchdog's control-socket `hello` includes what `install.json` records.
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
  `lsof` cannot see it). voiced's own running services are not a
  conflict.
- `[tts] espeak_library` and `espeak_data` config keys for the espeak-ng
  that Kokoro loads.
- Homebrew formula (`packaging/homebrew/voiced.rb`, for the
  `JulianBeaulieu/homebrew-voiced` tap): `brew install --HEAD` builds the
  `voiced` command and its environment from the shipped lock, then
  `voiced install` sets up the services with no `--source`. ADR 0017.
- One install layout for every install path (ADR 0017): `~/.voiced` is the
  state directory, `~/.voiced/bin/voiced` a link to the active environment,
  and `install.json` records it and `previous`, the environment the active
  one replaced, which pruning keeps and the app rolls back to (null for
  none). `voiced install` links the environment it runs from (Homebrew's
  keg through `opt`), or with `--source <checkout>` builds a fresh one into
  `~/.voiced/envs/` on a uv-managed Python in `~/.voiced/python`, and checks
  that it imports the engines before linking. Switching between Homebrew
  and a checkout is `voiced install` plus `voiced restart all`, with no
  root step; a `~/.voiced` from before 0.1.0 (a venv) is adopted and
  replaced on the first switch (`voiced install --source <checkout>`). A
  state directory that is not voiced's, or that holds files that are not
  (a venv of your own included), is refused and never deleted. Installs and
  uninstalls hold a lock on the state directory, and a failed install
  changes nothing.
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
  Installs from before 0.1.0 pick this up with `voiced install` and the
  root step it prints.
- Project skeleton: `src/voiced` package with a `uv` development setup,
  frozen engine interfaces (`STTEngine`, `STTStreamSession`, `TTSEngine`),
  entry-point engine registry, one config file plus `VOICED_*` environment
  overrides, bounded inference queue, `/health`, bearer authentication for
  `/v1`, `voiced serve` and `voiced config`, fake engines for tests, CI on
  `macos-14`.
- Installer and service lifecycle (#3): `voiced install` (service
  environment in `~/.voiced`, model download, the printed root step,
  `--dry-run`), `start`, `stop`, `status`, `logs`, `uninstall`, `doctor`
  with `--metal-probe`, and the `voiced watchdog` daemon that restarts hung,
  stalled or persistently degraded services, with `[watchdog]` and
  `[install]` config keys.
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
  a documented dependency.
- Parakeet TDT 0.6B v3 speech-to-text engine (#7), the default: batch
  transcription in overlapping 20 s chunks, streaming with partial
  transcripts and a finalize within milliseconds of the end of speech, and
  Silero VAD through `pysilero-vad` (a core dependency) in `voiced.vad`.
  Known limits: partials trail by about 0.7 s, and a pause of about one
  second can repeat a short word in a streamed transcript. Known miss:
  push-to-talk finalize (no trailing silence) has a median of about 114 ms
  against the 50 ms goal, since the tail is encoded after `end`; clients
  that can should stream 200 ms of silence before `end` (ADR 0008,
  ADR 0015).
- Soprano-80M text-to-speech engine (#8), opt-in: one voice, ignores
  `speed`, first audio in about 150 to 220 ms, with a repetition penalty and
  a per-sentence length bound against runaway generation. `scipy`, in the
  `tts` extra, resamples its 32 kHz output.
- Local control channel: the watchdog serves `~/.voiced/control.sock`
  (newline-delimited JSON, 0600, peer uid checked, nothing on the network)
  with status, settings, engine catalogues, restarts and log tails for a
  menu bar app. Commands `voiced restart` and `voiced settings get|set`;
  `voiced status` shows state, uptime and restarts when the watchdog answers,
  and `voiced doctor` checks the socket. Optional settings form on the status
  page behind the `[server] browser_settings` key (off by default; it always
  needs the API key). Settings writes keep comments (through `tomlkit`, a
  core dependency).
- End-to-end test tier (`tests/e2e/`, model tier) that runs the real
  services with real engines and drives them with the `openai` client, the
  WebSocket stream and the Wyoming client, and `scripts/bench.py`, which
  measures first audio, streaming finalize, batch transcription, memory and
  the speech-to-LLM-to-speech loop against live servers (ADR 0015).
- `/metrics.json` reports `memory.footprint_bytes` (the physical footprint,
  which includes MLX's GPU buffers) and `memory.mlx` (active, cached and peak
  allocation); the status page shows the footprint. Known miss: the idle
  footprint after use is 2.21 GB, within the 3 GB budget, but both engines
  peak above their idle allocation while they work, and if the two
  services' MLX peaks coincide their footprints would add to about 4.9 GB
  against the 3 GB budget. That is an estimate from the separate peaks, not
  a measurement (ADR 0015).
- Documentation: README with quickstart, architecture diagram, benchmarks
  and troubleshooting; API reference; Home Assistant guide; architecture and
  operations guides; ADR index; contributing guide; issue and pull request
  templates; Dependabot; manual `model-tests` CI job.

### Fixed

- What the Parakeet planner encoded could depend on where the audio was cut,
  not only on the audio: the call at a pause encoded everything fed so far
  (up to a frame past the last judged one, so sometimes more than a pass),
  and after a long pause the grid could restart at a point set by where a
  chunk ended. Each was rare with a client's fixed chunk size; with several
  streams the amount planned at once depends on timing, so a session could
  keep different windows than alone. Decisions are taken after each judged
  32 ms frame from judged positions only (ADR 0020).
- A streaming STT session could fail with `IndexError` when a pause outgrew
  2.5 s after a speech frame that reached just past the start of the
  planner's buffer (`_has_speech` and `_skip_to_speech` disagreed by half a
  frame).
- A push-to-talk finalize while Kokoro synthesized in the other service took
  up to 1.4 times as long in the median and 1.6 times at worst (Mac Studio:
  median 152 to 177 / worst 201 to 221 ms against 123 to 125 / 131 to 134
  alone): each of the STT encoder's short GPU round trips waited behind a
  whole buffer of Kokoro's work. The STT holds a per-user lock file for each
  encoder call a client waits on (the call at a pause, the calls after the
  end of the stream, and every call of a session that is behind), and
  Kokoro submits a sentence block by block and waits while the lock is
  held, at most 300 ms per sentence (ADR 0019). On the Studio the row is
  median 125 to 130 / worst 136 to 149 ms: within the 150 ms target,
  though a worst case of 149 ms is at it, with no margin.
  Known miss: Kokoro first audio while another client streams to the STT
  reaches 239 to 243 ms at worst (p95 158 to 200; 149 to 156 at worst
  without the lock) against its 200 ms target, when a reply starts during
  the other client's urgent tail; the STT is given the GPU there. Kokoro
  first audio alone is about 7 ms later, since every sentence is submitted
  block by block (median 92 to 93 against 85 to 86 ms).
- A streamed Wyoming session that failed closed the connection right after
  its `Error`: an STT session on `server_busy` after waiting for the slot,
  the utterance limit, a format change mid-stream or the wall-clock bound,
  and a TTS request whose engine failed mid-stream or whose text went over
  `tts.max_input_chars`. Home Assistant writes the whole stream (every
  chunk and the `AudioStop`, or the text and the `SynthesizeStop`) before
  it reads, so its next write met a reset and the `Error` could be lost.
  The server sends the one `Error`, reads and drops the rest of the stream,
  and closes at `AudioStop` or `SynthesizeStop`, at most
  `server.session_idle_s` after the failure. A refused `SynthesizeStart`
  has the same deadline.
- A streaming STT session that waited for the slot behind another (two
  satellites talking at once) failed with "streaming backlog over 30s" when
  its client had sent more than 30 s meanwhile. `add_audio` waits for the
  encoder past the backlog bound instead of failing, so the session catches
  up at about six times real time. A Wyoming session past `stt.streams`
  waits about 34 s for a slot before `server_busy`, so with
  `stt.streams = 1` and three satellites at once the third fails on
  utterances over about 31 s. ADR 0008 documents that one-at-a-time
  capacity (one stream within the finalize target, the rest queue).
- Under launchd the services were slower on short bursts of work than the
  same code started from a terminal: push-to-talk finalize was 145 ms median
  on an M1 Max against 114 ms. A launchd job is not in a terminal app's
  coalition, and macOS runs its first burst of work after an idle gap far
  below full speed. `voiced serve` sets its own task role to foreground
  application at start, which brings it to 122 ms (ADR 0015).
- `voiced restart all` restarted stt and tts but not the watchdog, which
  kept running the old code after an upgrade until it was killed by hand.
  It restarts the watchdog last (SIGTERM, then `launchctl kickstart`)
  and waits for the new one to answer on the control socket; `voiced
  restart watchdog` restarts it alone.
- `voiced install` built an invalid pip requirement (`voiced==X[stt,tts]`)
  for a pinned release; extras go before the version.
- The TTS engines never bounded MLX's buffer cache, so the Kokoro service
  grew to several GB after a few replies; the cache is capped at 256 MB
  and emptied after the warm-up.
- `voiced serve` with a port already in use exited 0, or only after loading
  the model; it names the port and exits 3 before loading anything.
- The `finalize` contract said the server's endpointer ends a streaming turn;
  the client's `end` message or Wyoming `AudioStop` does.
- `voiced doctor` reports the service environment's Python, including a
  broken link into a removed uv Python, and warns when the config file is
  readable by other accounts. `voiced status` says how to install the
  daemons when none are installed.
- The engine catalogue child process does not import modules from the
  watchdog's working directory.
- Wyoming failures are logged, with a traceback, like HTTP ones.
- README: the quickstart pins `--python 3.12` for `uv tool install`, gives
  measured download sizes, and lists the GPL-3, LGPL and MPL packages the
  extras pull in.

### Changed

- Push-to-talk finalize (no trailing silence) while other streams are open
  has the same median as with one stream but is worst 226 to 281 ms from
  two streams on, over the 150 ms target (ADR 0015, Known misses): the tail
  can wait for another session's encoder call that has just started. Alone
  it is as with `stt.streams = 1`. Set
  `stt.streams = 1` where most clients are push-to-talk and need the 150 ms
  worst case.
- ffmpeg comes from the `imageio-ffmpeg` wheel, a core dependency (a static
  ffmpeg 7.1, GPL-2.0-or-later), preferred over one on `PATH`; `brew install
  ffmpeg` is not needed. `voiced doctor` names the ffmpeg in use and
  where it comes from.
- voiced ignores a shell's `HF_HUB_CACHE`, `HUGGINGFACE_HUB_CACHE` and
  `HF_ENDPOINT`, like `HF_HOME`: the models come from the hub into the
  cache `[install] hf_home` names.
- The `tts` extra needs `huggingface-hub` 1.1 or later.
- `voiced install` does not write plists or `install-daemons.sh` to
  `~/.config/voiced/launchd/`; it removes what installs before 0.1.0 left
  there and prints one line,
  `sudo /bin/bash <environment>/.../voiced/launchd/voiced-privileged.sh install <user>`,
  which runs the privileged script the menu bar app runs (ADR 0018).
  `voiced uninstall` prints the same with `uninstall`. The `--staging-dir`
  flag is gone, and the `done` progress event carries no `staging_dir`. An
  install with another state directory, config path or
  `[launchd] label_prefix` gets no root step; `label_prefix` only names the
  labels the CLI and the watchdog address. `voiced doctor --metal-probe`
  writes one script, `~/.config/voiced/metal-probe.sh`, with the probe's
  plist inside it. Installs from before 0.1.0: run the printed line once.
- The LaunchDaemon plists carry only a fixed environment (`HOME`, `PATH`,
  `VOICED_CONFIG`, `PYTHONUNBUFFERED`, `VOICED_STDIO_LOG` for the default
  log directory), the same values the menu bar app's privileged step
  renders. The services take `HF_HOME` (`[install] hf_home`), the espeak-ng
  paths and a custom `[server] log_dir` from the config file at start, and
  these must be absolute (or start with `~`). Every `voiced` command sets
  `HF_HOME` and `VOICED_ESPEAK_*` from the config alone: values exported in
  your shell are ignored (with a notice for `VOICED_ESPEAK_*`), so the
  services, `doctor` and the installer's model download agree. If an
  install from before 0.1.0 relied on the shell's, re-run `voiced install`
  and the root step it prints.
- `voiced install --source` runs uv with `UV_NO_CONFIG=1`, so a `uv.toml`
  of yours cannot change what is installed.
- `voiced uninstall` removes only what the install layout owns, keeping the
  rest; before 0.1.0 a directory with `pyvenv.cfg` was deleted whole.
- `voiced install --source` takes a checkout path (not a pip requirement)
  and needs uv; `--no-uv` is gone, and `--state-dir` replaces `--venv` (the
  old name still works). `[install] python` is a version.
- `voiced doctor` recognises voiced's daemon on a port by its argv[1], the
  entry point.
- CI uses `actions/checkout@v7` and `astral-sh/setup-uv@v10.3.0`, installs
  ffmpeg, and cancels superseded runs of the same branch.
- `parakeet-mlx` is pinned to `>=0.5,<0.6` and `mlx-audio` to
  `>=0.5.8,<0.5.10`, the releases the engines were checked against.
- Uploads that decode to more than about 26 minutes (1562 s) of audio get
  413: the decoded audio is bounded by `stt.max_upload_mb` read as 16-bit
  16 kHz samples (1562 s at the default 50). A 30-minute mp3, which was
  transcribed before 0.1.0, is refused. Raise `max_upload_mb` to transcribe
  longer recordings (it bounds Wyoming utterances and WebSocket streams the
  same way).

### Security

- Root does not copy files the user's account can write. Before 0.1.0
  the CLI's generated `install-daemons.sh` copied plists staged in
  `~/.config/voiced/launchd/` into `/Library/LaunchDaemons` unchecked, so
  any process of the user's could rewrite one after the script was read
  and get a program run as root at boot; its `uninstall-daemons.sh` removed
  another account's daemons. Both are replaced by the menu bar app's
  privileged script, which renders the plists as root from the user name
  alone, refuses system accounts and another account's install, takes a
  lock and calls every tool by path. The CLI prints its line, with the
  path resolved, only when no account but the user's and root can change
  the script or a directory on its path, by mode bits or ACL (not, for
  example, from a Homebrew another admin owns). The Metal probe's script
  uses the fixed label prefix and touches its result file only as the user.
- The LaunchDaemon plists do not set `StandardOutPath` or
  `StandardErrorPath`, as they did before 0.1.0: launchd may open those
  files as root in the user-owned log directory, so a symlink planted there
  could make root create or append to any file. The daemons open
  `<service>.stdout.log` and `.stderr.log` themselves, as the user, refuse
  a symlink, and point their stdout and stderr at them (`VOICED_STDIO_LOG`
  in the plist). Installs from before 0.1.0 keep the old plists, and the
  exposure, until the root step `voiced install` prints is run again.
- Uploads cannot make ffmpeg open other files: ffmpeg reads the upload
  through an inherited descriptor and may open nothing else, so an
  `ffconcat` list or playlist is refused. Upload files are anonymous and
  never left behind. The decoded audio is bounded like a Wyoming utterance
  (413), and at most `queue_depth` uploads decode at once (503). ffmpeg must
  support its `fd` protocol, which `voiced doctor` checks.
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
  upgrading from before 0.1.0, run it once (or
  `chmod 600 ~/.config/voiced/config.toml`).
- `voiced serve` raises its open file limit from launchd's 256 to 4096.

[0.1.0]: https://github.com/JulianBeaulieu/voiced/releases/tag/v0.1.0
