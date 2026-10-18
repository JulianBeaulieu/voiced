# 0018: Install from the menu bar app

Date: 2026-10-15, revised 2026-10-18. Status: proposed.

## Context

Until now, installing voiced has meant a terminal: Homebrew for ffmpeg and
espeak-ng, `uv`, `voiced install --source ...`, then `sudo bash` on the
printed script (ADR 0006). The owner decided that people install the menu
bar app and the app installs the daemons. The Homebrew cask installs the
app, the app installs and manages the services, and the app is where
progress and errors are shown. Permissions are asked for only when a step
needs them, each with a one-line reason, and the app never uses the
microphone. The `voiced` CLI stays for headless and advanced use, and the
Homebrew formula (ADR 0017) stays the terminal path.

The app (ADR 0014) talks only to the watchdog's control socket and reads
`/Library/LaunchDaemons` to tell "not installed" from "not running". With
this decision it also has to put Python, the voiced environment, ffmpeg,
espeak-ng and the models onto a Mac that may have no Homebrew and no
terminal user, and get three LaunchDaemons approved. Those daemons run as
the installing user, never as root, and start at boot before anyone logs
in (ADR 0004).

This record is the design; the implementation comes later. Everything marked
*measured* was run on an M1 Max with macOS 15.7.4, uv 0.13.1 and Xcode 26.3,
inside a temporary `HOME`, uv cache, uv Python directory and Hugging Face
cache. Nothing was written to `~/.voiced`, `/Library` or the user's caches,
and nothing ran with sudo. The prototypes are in `menubar/Prototypes/` and
are not part of the app.

## One layout for every install

Homebrew, CLI and app installs share one layout, and ADR 0017 owns its
rules in one section; this record cites them rather than restating them.
In short:

- `~/.voiced` is the state directory, not necessarily a venv. It holds
  `bin/voiced`, a symlink to the active environment's `bin/voiced`;
  `envs/<id>/`, the environments built by the app or by `--source`;
  `control.sock`; and `install.json`.
- `install.json` records `{source: cli|homebrew|app, environment, version,
  lock_sha256, python}`. Uninstall uses it to know what is voiced's,
  `voiced install` to adopt or refuse, and the app to detect a version or
  lock mismatch.
- `voiced install` has one mode: without `--source`, the environment is the
  `sys.prefix` of the `voiced` that runs it, and it links `bin/voiced` to
  it. The app therefore runs `<its environment>/bin/voiced install
  --json-progress`.
- The plists are identical for every source and run `~/.voiced/bin/voiced`.
  Switching sources or versions is repointing that symlink and
  `voiced restart all`: never root, never a delete.
- One lock for every path: `uv export --frozen --no-dev --extra stt
  --extra tts --no-emit-project`, shipped as package data
  (`voiced/requirements.lock`, checked against `uv.lock` in CI), installed
  with `install --require-hashes --no-deps -r <lock>` and then voiced with
  `--no-deps`. `install`, not `pip sync`: sync would remove third-party
  engine plugins (ADR 0013).
- The watchdog restarts both services and exits when its environment
  disappears or `realpath(~/.voiced/bin/voiced)` changes, so a swapped
  environment takes effect without a separate step.
- A legacy install, where `~/.voiced` itself is a venv, is adopted with
  `environment = ~/.voiced`.

What this record adds for the app: it keeps uv's state in two places only,
`~/.voiced/python/` (`UV_PYTHON_INSTALL_DIR`, the CPython the environments
link to, for app and CLI installs alike) and
`~/Library/Application Support/voiced/uv-cache/` (`UV_CACHE_DIR`).

## The privileged step

Three LaunchDaemon plists have to reach the system domain. That needs an
administrator once at install, again at uninstall, and again whenever a
plist's contents change. Upgrading the environment, changing a setting and
`restart all` need no root (ADR 0012).

### (a) One administrator prompt for a script in the signed executable: chosen

The app runs the root half of the install through the standard
authorization dialog: `NSAppleScript` with `do shell script "..." with
administrator privileges` (Apple TN2065). That command is not deprecated
and notarization does not flag it. Internally it goes through
`AuthorizationExecuteWithPrivileges`, and the root shell inherits the
app's environment: `PATH`, a planted `BASH_ENV` and test variables all
reached it when measured. So the command it runs is always

```sh
/usr/bin/env -i PATH=/usr/bin:/bin:/usr/sbin:/sbin \
    /bin/bash --noprofile --norc -c "$SCRIPT" voiced-privileged <mode> <args>
```

with every tool inside the script called by its absolute path. `$SCRIPT`
is a string constant compiled into the app's main executable, not a file in
the bundle. A cask install puts the app in `/Applications` owned by the
user, so any process of that user can edit a script file inside the bundle;
the main executable's text is covered by the code signature, and the copy
that runs is the one the signed, running process loaded. Before showing the
prompt, the app checks its own bundle with `SecStaticCodeCheckValidity`
(strict validation, all architectures; in release builds also against the
designated requirement with voiced's team identifier) and refuses to ask
for an administrator if the check fails.

This narrows the risk; it does not remove it. A process running as the
user can still replace the whole app with its own and show the same dialog,
or show a look-alike dialog of its own. That is the same-user attacker ADR
0016 accepted: one who can already run code as the user and wait for them
to type a password. What the design guarantees is that root never runs
text a user process wrote, and that the prompt the user sees came from an
intact voiced.

The prototype is `menubar/Prototypes/PrivilegedInstall/voiced-privileged.sh`,
one script with two modes and shared validation:

```
voiced-privileged install   USER STT_B64 TTS_B64 WATCHDOG_B64
voiced-privileged uninstall USER
```

1. The plists arrive as base64 arguments. Root never opens a path the user
   controls, so there is no symlink or FIFO to follow.
2. The user must exist, have a uid of 500 or more and a home directory,
   which is read with `dscl -plist` so a home with spaces works. `root` is
   refused.
3. The labels are hard-coded (`io.github.julianbeaulieu.voiced.{stt,tts,watchdog}`).
   They are never taken from the caller, who could otherwise name another
   product's daemon.
4. **The security boundary is `UserName` plus a key allowlist**, not the
   program path. A plist may contain only `Label`, `ProgramArguments`,
   `UserName`, `RunAtLoad`, `KeepAlive`, `ThrottleInterval`,
   `StandardOutPath`, `StandardErrorPath`, `ProcessType`,
   `WorkingDirectory`, `EnvironmentVariables` and
   `AssociatedBundleIdentifiers`; anything else (`Program`, `Sockets`,
   `MachServices`, `Nice`, `SessionCreate`, `GroupName` ...) is refused.
   Each value is checked exactly: `UserName` is the user;
   `ProgramArguments` is `<home>/.voiced/bin/voiced`, the one entry point
   of the shared layout, followed by `watchdog` or `serve <service>` and
   nothing else; the log
   paths are `<home>/Library/Logs/voiced/<service>.std{out,err}.log`;
   `WorkingDirectory` is the home; `RunAtLoad` and `KeepAlive` are `true`.
5. The installed plist is rebuilt from the checked values, written to a
   root-owned 0700 temporary directory, and made 0644 only when it is
   complete.
6. **One install per Mac.** If `/Library/LaunchDaemons/<label>.plist`
   exists with another `UserName`, both modes refuse, and the app says
   "voiced is already installed for <user> on this Mac."
7. Only after all three plists pass does it touch launchd: per label,
   boot out, wait until the label is gone, move into place, bootstrap. A
   bootstrap that fails is reported per label (exit 3) and the others stay
   as they are; the app offers Repair. A trap removes every temporary file
   on any exit. A refusal never writes anything to `/Library`; a launchd
   failure halfway can leave a label booted out, which is why the result
   is per label.
8. When the application firewall is on, it allows the services' Python
   (see Permissions).

*Measured* with `menubar/Prototypes/PrivilegedInstall/test.sh`, run as a
normal user against a scratch destination with launchctl skipped (the
destination override is ignored when the script runs as root): the plists
staged by today's `voiced install` install, and the installed copy has only
allowlisted keys. Refused with nothing written: `ProgramArguments[0] =
$HOME/../../bin/sh`, a `Program`, `Sockets` or `MachServices` key,
`StandardOutPath = /etc/sudoers.d/voiced`, `Nice = -20`, `SessionCreate`,
`UserName root`, no `UserName`, `KeepAlive` as a dictionary, an extra
argument, `Label com.docker.vmnetd`, a key containing a newline, any
program other than `~/.voiced/bin/voiced`, user `root`, input that is not
base64. Another user's install is neither replaced nor removed. 21 of 21
cases pass.

The prompt says what it is for (`with prompt "voiced needs an
administrator to install its background services."`). Cancelling it
(error -128) is not an error: the app goes back to the "Install background
services" screen. This path works unsigned, so development builds take it
too. It keeps the `UserName` daemons that ADR 0004 measured, including
starting at boot before login; with FileVault they start once the disk is
unlocked, as today.

During implementation, one probe needs the owner at the keyboard: run the
admin prompt once with `BASH_ENV` set in the app's environment and confirm
the `env -i` line keeps it out of the root shell.

### (b) `SMJobBless` privileged helper: rejected

It is deprecated since macOS 13, needs a signed helper and a
signing-requirement handshake, and leaves a root process installed
permanently just to copy three files a few times in voiced's lifetime.

## Provisioning without a terminal

### What the app ships and pins

The release job builds, from one commit:

- `Contents/Helpers/uv` (pinned version and checksum, re-signed with
  voiced's identity and the hardened runtime, MIT or Apache-2.0 licence
  text in `Contents/Resources`). *Measured*: uv 0.13.1 for arm64 is 27.8 MB,
  14.3 MB compressed, and links only system libraries.
- The voiced wheel (198 KB), and a copy of the lock it carries as package
  data (ADR 0017's `voiced/requirements.lock`, exported from `uv.lock`,
  never re-resolved). *Measured*: a fresh `uv pip compile` already differed
  from `uv.lock` in five packages (mlx 0.32.3 against 0.32.4 among them);
  the export matches `uv.lock` exactly, and a hash-checked install from it
  takes 6 s with a warm cache. The app path therefore needs no GitHub
  archive, no hatchling build and no PyPI release of voiced.
- A manifest (`install-manifest.json`): the CPython patch version
  (`3.12.15`), the lock's sha256, the wheel's name and sha256, the total
  download size of the locked wheels, and each default model's repository,
  commit SHA and size. The welcome screen's numbers and every pin come from
  it.

### Python and the environment

| Step | Command (bundled uv, `UV_NO_CONFIG=1`, `UV_LINK_MODE=clone`) |
|---|---|
| Python | `uv python install --no-bin 3.12.15` into `~/.voiced/python`. `--no-bin` because without it uv wrote `~/.local/bin/python3.12` onto the user's `PATH` (measured). The patch version, because the `3.12` minor link moves silently on the next install |
| Environment | `uv venv --managed-python --python 3.12.15 ~/.voiced/envs/<id>.partial`, renamed to `envs/<id>` when the packages are in; `<id>` is the version and the lock hash |
| Packages | `uv pip install --require-hashes --no-deps -r requirements.lock`, then `uv pip install --no-deps <voiced wheel>` |

*Measured* cold, with an empty uv cache, no Homebrew on `PATH`
(`/usr/bin:/bin:/usr/sbin:/sbin`) and a fresh `HOME`: Python 3 s (71 MB),
venv 1 s, packages 21 s (83 distributions, venv 680 MB, about 205 MB of
wheels). 27 s wall clock. All wheels are arm64 binaries; nothing compiles.
The largest are mlx-metal (45 MB), llvmlite (41), scipy (29), onnxruntime
(21), transformers (13) and numpy (12). On APFS uv clones files from its
cache, so cache and venv share most blocks.

`voiced install --source` uses the same uv layout: it sets
`UV_PYTHON_INSTALL_DIR` under the state directory, `UV_NO_CONFIG=1` and
`--managed-python`, so a CLI install's Python can no longer be removed by an
unrelated `uv python uninstall` (the root cause behind ADR 0016 finding 15).
Today it runs `uv pip install --upgrade`, which re-resolves (`install.py`,
`venv_commands`); under ADR 0017's one-lock rule it installs from the lock
like every other path.

### ffmpeg: the pinned wheel, preferred

voiced uses ffmpeg to decode uploads and to encode `mp3` and `opus` (ADR
0007). *Measured*: the `imageio-ffmpeg` 0.6.0 wheel for macOS arm64 is
21.1 MB and contains a static ffmpeg 7.1 (49 MB, `--enable-gpl
--enable-libmp3lame --enable-libopus`) that links only system frameworks.
With it, decoding wav, mp3, m4a, ogg, flac, webm, caf and aiff works, and
`ffconcat` and `m3u8` inputs are refused as before. Implementation items:

- `imageio-ffmpeg` becomes a core dependency, not part of `stt`: TTS needs
  it to encode too.
- `ffmpeg_path()` returns `imageio_ffmpeg.get_ffmpeg_exe()` first and a
  `PATH` ffmpeg only if the wheel is missing, so every Mac runs the build
  the release was tested with.
- `doctor` checks `ffmpeg_path()`, not `which("ffmpeg")`, and its hints
  stop saying `brew install ffmpeg`.

### espeak-ng: the wheel's copy, cloned to a short path

`kokoro-onnx` already depends on `espeakng-loader`, so every voiced venv has
espeak-ng 1.52 (a 0.5 MB dylib and 21 MB of data). ADR 0009 avoided that
copy because it breaks on long data paths. The root cause is a 160-byte
path buffer in the `espeakng-loader` build of the library: the resolved data
path must be at most 159 bytes. Homebrew's espeak-ng has no such limit.
*Measured* with the engine's own `_load_espeak`:

| Resolved data path | Result |
|---|---|
| 52 to 152 bytes | correct phonemes |
| 162 and 182 bytes | `Error processing file '.../phontab'`, then espeak calls `exit(1)`: the whole process ends, no `except` catches it, and a truncated path can even open a different directory |
| any length, through a symlink | no help: phonemizer resolves the link first |

An app install's data path is `~/.voiced/envs/<id>/lib/python3.12/site-packages/espeakng_loader/espeak-ng-data`:
109 bytes for a 14-character user name, so a longer user name or a custom
`venv_dir` crosses the limit. Used in place it would work on most Macs and
kill the TTS service on some.
Implementation item: at each load the engine clones `espeak-ng-data` with
`/bin/cp -cR` (an APFS clone, no extra blocks) into the per-user cache
directory from `os.confstr(65538)` (`CS_DARWIN_USER_CACHE_DIR`, e.g.
`/private/var/folders/6p/<random>/C/voiced/espeak-ng-data`, 78 bytes and
independent of the user name and `venv_dir`), then checks the resolved
length is at most 159 bytes and refuses to load with a `doctor` message
otherwise, rather than letting espeak exit. Re-cloning each load also
covers the system clearing that directory. *Measured*: the clone at that
path phonemizes correctly. The wheel's copy comes first, so every Mac runs
the version in the lock; Homebrew's keg (first in ADR 0009) becomes the
fallback, and the explicit `VOICED_ESPEAK_*` variables still override both.

### Models: pinned and downloaded with real progress

The defaults are `mlx-community/parakeet-tdt-0.6b-v3` (one file of
2508.5 MB) and `mlx-community/Kokoro-82M-bf16` (355.3 MB in 56 files without
the sample WAVs), 2.86 GB together; *measured* 290 s with both engines
loaded once, unauthenticated, on this connection. Implementation items:

- Each app version pins each default model to a commit SHA from its
  manifest (`revision=`), so a model repository changing upstream cannot
  change an installed version.
- A new engine method `download_patterns()` names the files the engine
  needs. `voiced install` downloads with
  `huggingface_hub.snapshot_download(repo, revision=..., allow_patterns=engine.download_patterns(), tqdm_class=...)`,
  so the byte progress is real and Kokoro's sample WAVs are not fetched.
  `engine.load()` takes no progress hook; it then loads from the cache.
- An interrupted download resumes from the `.incomplete` files on the next
  run.

### Licences

The app bundle contains only MIT code (voiced, its wheel) and uv (MIT or
Apache-2.0). The following are installed from PyPI by the user's own
machine at install time, are not part of voiced's MIT code and are never
in the app, the cask or a release asset:

| Component | Licence | Comes from |
|---|---|---|
| phonemizer | GPL-3.0-or-later, imported into the TTS process | PyPI |
| espeak-ng 1.52 | GPL-3.0-or-later; the wheel carries no licence text | `espeakng-loader` |
| ffmpeg 7.1, GPL build with x264, LAME and Opus | GPL | `imageio-ffmpeg` |
| soxr, zeroconf | LGPL-2.1-or-later | PyPI |

The README gains a third-party section and the repository a `NOTICE` file
listing these with links to their sources; the app's About pane shows the
same list. The release job must never mirror these wheels or offer an
offline installer that contains them: that would make the project a
distributor of GPL object code, with the duty to provide the corresponding
source for that exact build. This is the project's reading, not legal
advice.

### What a first install costs

| | Download | On disk |
|---|---|---|
| app (with uv) | about 16 MB zipped | about 30 MB |
| Python | about 25 MB | 71 MB |
| voiced environment | about 205 MB | 680 MB, plus the uv cache that shares its blocks |
| models | 2.86 GB | 2.86 GB |

The disk check before installing uses ADR 0016's measured 4.4 GB for a
whole install, not the 3.1 GB sum above, because the uv cache, the
`.incomplete` downloads and the Python download all take room while the
install runs.

## Flow

The app's existing states (ADR 0014) gain an installer in front of them.
"Not installed" opens the installer instead of the guide link.

0. **Before anything.** If the app is translocated
   (`SecTranslocateIsTranslocatedURL`, an app opened from Downloads
   without moving it), it asks to be moved to `/Applications` first; a
   translocated path is random and read-only. Moving the app later is fine:
   the daemons do not run anything from inside it.
1. **Welcome.** What voiced is, what goes where (`~/.voiced`, models in the
   Hugging Face cache, three background services that run as *you*), the
   download size and disk space from the manifest, and whether the disk has
   room. If an install already exists, this screen says so instead (see
   Lifecycle). One button: "Install".
2. **Environment** (no prompt): the steps in the table above. uv has no
   machine-readable progress, so this step shows an indeterminate spinner
   with the current step's name, not an invented bar. It takes about 27 s.
3. **Models** (no prompt):
   `~/.voiced/envs/<id>/bin/voiced install --json-progress --app-bundle-id io.github.julianbeaulieu.voiced.menubar`
   checks the environment imports the engines, writes the config if absent
   and the log directory, checks the configured ports are free, downloads
   the models with byte progress, loads each engine once, stages the
   plists, and finally links `~/.voiced/bin/voiced` to its own environment
   and writes `install.json` with `source = app`.
4. **Approval**: one administrator prompt runs `voiced-privileged install`.
5. **Starting**: the app waits for the watchdog's control socket and its
   `hello` (up to two minutes; model loading is slow on a cold disk).
6. **Connect Home Assistant.** "Use voiced with Home Assistant on another
   device?" Yes sets `bind = lan` and `zeroconf = true` through the control
   socket (the watchdog validates and restarts, ADR 0013). The screen then
   shows the Mac's `<host>.local` name and IP address, the Wyoming ports
   (speech to text 10300, text to speech 10200) with copy buttons, and
   "Home Assistant finds voiced under Settings > Devices & services >
   Discovered." It offers "Generate API key" and says plainly that the key
   protects the HTTP API only; Wyoming has no authentication, so anyone on
   the network can use those two ports. The Network pane in Settings shows
   the same host, address and ports. No keeps voiced on this Mac only.
7. **Done.** "Open voiced at login" checkbox, off by default; then the
   normal menu.

### Failure states

Each step fails on its own and shows the step, a one-line cause, the last
lines of output and "Try again". Running a step again is safe: uv reuses
its cache, `voiced install` keeps an existing config, and the privileged
script rebuilds and replaces the plists.

| Situation | What the user sees and what happens |
|---|---|
| No network | "Can't reach the internet." before the first download, checked against PyPI and Hugging Face |
| Not enough disk | Welcome says how much is needed (4.4 GB) and how much is free; Install is disabled |
| Admin prompt cancelled (-128) | Back to "Install background services", no error |
| Port in use | `voiced install` checks the configured ports before approval and names the process holding one |
| Download interrupted | "Try again" resumes from the `.incomplete` files |
| App quit mid-install | The app terminates its children (SIGTERM, then SIGKILL after 5 s). The next launch reads progress from disk (a `.partial` environment is deleted, a complete one in `envs/` is kept, cached models count as done, installed plists mean only Starting is left) and offers "Continue" |
| One label failed to bootstrap | Names the service; offers Repair |
| Another user's install | "voiced is already installed for <user> on this Mac." Nothing else is offered |

### The progress protocol

`voiced install --json-progress` writes one JSON object per line on stdout;
human output goes to stderr.

- The first line is `{"event": "hello", "protocol": 1, "version": "<voiced version>"}`.
  The app refuses any other protocol number with "Update voiced" or
  "Update the app", whichever side is older. Unknown events and unknown
  fields are ignored, so adding either keeps protocol 1.
- `{"event": "step", "step": "env|config|ports|models|staging|link", "state": "start|done|skipped"}`
- `{"event": "download", "model": "<repo>", "bytes": n, "total": n}`, with
  the total from the manifest, throttled to four a second.
- `{"event": "log", "line": "..."}`
- The last line is `{"event": "done", "staging_dir": "..."}` or
  `{"event": "error", "step": "...", "message": "..."}`, and the exit code
  agrees with it (0 only with `done`).

## Lifecycle

### Updates, with rollback

The app compares its own pins with the running services. The watchdog's
`hello` reports what `install.json` records (`version`, `lock_sha256`,
`python`; added fields, so protocol 1 still applies), and without a
watchdog the app reads `~/.voiced/install.json` itself. Any difference, in
either direction, is a mismatch, and the app's lock wins: an older app on a
newer environment offers to install its own pins too, so app and services
are always one version (ADR 0014).

"Update voiced" then:

1. builds a new environment beside the old one (`envs/<id>.partial`,
   renamed when its packages are in);
2. runs the new environment's `voiced install --json-progress ...`, which
   smoke-tests it (`voiced --version` and the engine imports), downloads
   any changed model revision, stages the plists, and only then repoints
   `~/.voiced/bin/voiced` and rewrites `install.json`;
3. runs `voiced-privileged install` only if the rebuilt plists differ from
   the installed ones or a label is not loaded. The app finds out without
   root: the plists in `/Library/LaunchDaemons` are world-readable and
   `launchctl print system/<label>` needs no root (measured);
4. waits for a `hello` with the new lock hash. The watchdog restarts the
   services and itself when the symlink changes (ADR 0017); the app runs
   `voiced restart all` only if that has not happened within the grace.

If step 2 or 4 fails, "Roll back" runs the previous environment's own
`voiced install --json-progress`, which points the symlink back at it
(models are cached, so it is quick). After a successful update the app
keeps the previous environment for one-click rollback, deletes any older
one, removes Python versions no remaining environment uses, and clears the
uv cache: the cache only grows (706 MB to 928 MB after one update,
measured) and `uv cache prune` finds nothing to remove, while the next
update re-downloads only what changed.

### An install that already exists

The app reads `~/.voiced/install.json` and the installed plists in
`/Library/LaunchDaemons` (whose `UserName` says whose install it is), and
follows ADR 0017's adopt-or-refuse rules:

| Found | The app |
|---|---|
| plists for another user | refuses (one install per Mac) |
| `source = app` | manages it: the normal case |
| `source = homebrew` | controls it through the socket and leaves the environment to Homebrew: Update says "Updated by Homebrew: `brew upgrade voiced`". "Switch to updates from the app" builds an app environment and runs its `voiced install`, which repoints the symlink; config and models stay, and the keg is untouched until the user runs `brew uninstall voiced` |
| `source = cli` (a `--source` environment in `envs/`) | the same: controls it, and switches only when asked. If that environment's voiced was installed from a local checkout (an editable or `file://` install in `direct_url.json`), the app says so before switching |
| a legacy install (`~/.voiced` itself is a venv) | adopts it as `environment = ~/.voiced`, controls it as is, and builds no second environment until the user updates. The first update switches as above; if the legacy plists ran a custom `venv_dir`, that update's privileged step rewrites them to `~/.voiced/bin/voiced` |
| staged plists but none installed | continues at Approval |

Switching never needs root (the plists do not change) and never deletes the
previous environment on the spot; it becomes the rollback target and is
pruned on the next update, legacy files by ADR 0006's rules.

### Repair

Shown when the plists are installed but the socket does not answer after
the startup grace, when the services report an environment error, or after
a failed bootstrap. It runs uv's `pip install --reinstall --require-hashes
--no-deps -r` of the bundled lock and uv's `pip install --reinstall
--no-deps` of the bundled wheel into the active environment, then that
environment's `voiced install --json-progress`. If the environment's Python
is gone, it builds a fresh environment instead, exactly as an update does.
The administrator prompt appears only if the plists differ or a label is
not loaded.

### Uninstall and removing the app

"Settings > About > Uninstall voiced…" runs `voiced-privileged uninstall`
(one prompt), then `voiced uninstall --yes`. Models (with their size), logs
and config are separate checkboxes, unticked by default, as in ADR 0006.
The privileged uninstall does not need a working environment: it takes the
labels from the hard-coded list, removes only plists whose `UserName` is
this user, and deletes nothing else. `voiced uninstall` deletes only what
`install.json` and ADR 0006's rules say is voiced's (environments only if
they contain `pyvenv.cfg`), plus `~/.voiced/python`; a Homebrew keg is never
touched. If the active environment is broken, the app runs the CLI from the
other environment in `envs/`, and failing that it says which directories
remain.

Moving voiced.app to the Trash leaves voiced running; the daemons do not
need the app. The welcome text and the README say: use Uninstall in
Settings first. Without the app, `voiced uninstall` (or its printed sudo
script) still works from a terminal.

The cask adds:

```ruby
uninstall quit:      "io.github.julianbeaulieu.voiced.menubar",
          launchctl: ["io.github.julianbeaulieu.voiced.stt",
                      "io.github.julianbeaulieu.voiced.tts",
                      "io.github.julianbeaulieu.voiced.watchdog"],
          delete:    ["/Library/LaunchDaemons/io.github.julianbeaulieu.voiced.stt.plist",
                      "/Library/LaunchDaemons/io.github.julianbeaulieu.voiced.tts.plist",
                      "/Library/LaunchDaemons/io.github.julianbeaulieu.voiced.watchdog.plist"]
zap trash: "~/Library/Application Support/voiced"
```

so `brew uninstall --cask voiced` stops the daemons and `--zap` removes the
uv cache. Neither touches `~/.voiced`, the config or the models; the cask's
caveats say so.

### Updating the app itself

Through the cask only (`brew upgrade --cask voiced`), or a new download
from the GitHub release. The app does not update itself, and there is no
Sparkle: one more signing key and feed for a tool whose users mostly come
through Homebrew is not worth it yet. A new app version finds the mismatch
above on its next launch and offers "Update voiced".

## Where the logic lives, and the change to ADR 0014

Swift orchestrates and Python keeps the knowledge. The app runs three kinds
of child process: the bundled `uv` (environment, update, repair), the
environment's `voiced` CLI as the user (`install --json-progress`,
`uninstall`, `restart all`), and the privileged script through one
authorization prompt. Paths, plist contents, the model list and every
uninstall safety rule from ADR 0006 stay in Python, so the CLI and the app
cannot install different things; the privileged script only checks and
rebuilds what Python staged.

This deliberately amends ADR 0014, and ADR 0014's text now says so: the app
runs `launchctl` only inside its signed, root-run privileged script, and
reads (without root) whether a label is loaded. Control (status, settings,
restarts) still goes only through the control socket, and the app still
never edits the config file.

## Permissions

The app asks for a permission only when the step that needs it comes up,
and says why in one line. If it does not need something, it never asks.

| Permission | Needed? | When and why | What the user sees |
|---|---|---|---|
| Administrator authorization | Yes | Approval, uninstall, and an update or repair whose plists changed: writing to `/Library/LaunchDaemons` needs root. | Before the system dialog: "To start voiced at boot, macOS needs an administrator to install three background services. They run as you, not as root." |
| Application firewall exception | Only if the firewall is on | Same prompt as above, no second one. The services' Python is signed ad hoc, and nobody can answer the firewall's "accept incoming connections" prompt before login, so Home Assistant on the LAN would be silently blocked. If `socketfilterfw --getglobalstate` reports it on, the privileged step adds and unblocks the resolved path of the active environment's Python (resolved as the user, through `~/.voiced/bin/voiced`), but only if it lies inside the user's home, as it does for app and `--source` environments. It runs again whenever that path changes (a new Python version). A Homebrew environment's Python is shared with every brewed Python program, so the app does not allow it and says so; that user can allow it in System Settings > Network > Firewall. | One line on the approval screen: "Your firewall is on; voiced will allow its services to accept connections." |
| Background items (System Settings > Login Items > Allow in the Background) | Not asked; macOS shows it | macOS 13+ lists every LaunchDaemon here and posts "Background Items Added". The plists name the app in `AssociatedBundleIdentifiers`, so the entry shows as voiced with its icon. If the user turns it off, launchd will not run the services. | After install: "voiced appears in Login Items as a background item; leave it on." If it is off, the menu says so and offers "Open Login Items" (`SMAppService.openSystemSettingsLoginItems()`). |
| Open at login (`SMAppService.mainApp`) | Optional | Only the menu bar icon needs it. The services run without the app. | A checkbox on the Done screen, off by default, also in Settings > General. |
| Notifications | No | Status is in the menu bar icon. If "notify me when voiced stops" is added later, the app asks then, from that toggle. | Nothing |
| Microphone, camera, screen recording | No | Audio comes in over HTTP or Wyoming from satellites and Home Assistant; the app never captures. No usage-description keys go in `Info.plist`, so it cannot even ask. | Nothing |
| Accessibility, Full Disk Access, Automation | No | `do shell script` runs inside the app and sends no Apple Events to other apps. The app reads only `~/.voiced`, `~/Library/Logs/voiced`, `~/.config/voiced` and `/Library/LaunchDaemons`, none of which privacy protection covers. | Nothing |
| Files in Documents, Desktop, iCloud Drive or a removable volume | No, unless configured there | An `HF_HOME` or `venv_dir` set under one of these is behind privacy protection (TCC), which a daemon cannot be prompted for. | Nothing; `voiced doctor` names such a path as the problem. |
| Local Network | No | The app connects only to its Unix socket. Local Network privacy does not apply to launchd daemons (TN3179), so the services' LAN listeners need no prompt. | Nothing |
| Keychain | No | The API key is in the config file, which is mode 0600 (ADR 0013). | Nothing |

## What changes

### Python side (`voiced`)

- The shared layout, `install.json`, the one install mode and the
  watchdog's self-restart, as ADR 0017 specifies them.
- `voiced install --json-progress` with the versioned protocol above.
- `voiced install --app-bundle-id ID` adds `AssociatedBundleIdentifiers`.
- No other new flags: no `--skip-venv` (the one install mode already uses
  the running environment), no `--uv`, no `--remove-uv-state` (the uv state
  lives in the state directory and goes with it), no `--version --json`
  (`hello` and `install.json` carry the version). `voiced uninstall
  --json` is optional.
- The watchdog's `hello` adds `install.json`'s version, lock hash and
  Python.
- The CLI's uv layout: Python under the state directory,
  `UV_NO_CONFIG=1`, `--managed-python`.
- The ffmpeg, espeak-ng and model items above, each with tests:
  `imageio-ffmpeg` in core dependencies and preferred; the espeak-ng clone
  and length check; `download_patterns()`, pinned revisions and
  `snapshot_download` progress; a port check before approval.
- The sudo script printed by the CLI stays for terminal users. It should
  gain the firewall step and the same checks; that is a follow-up.

### App side

- An `Installer` module in `VoicedMenuBarUI`: a state machine over the
  steps above, testable with a fake runner the way `FakeControlServer` is
  used today; the welcome, progress, Connect Home Assistant and done views.
- `Contents/Helpers/uv`, the voiced wheel and a copy of its
  `requirements.lock`, `install-manifest.json`, the uv licence and the
  third-party notice in `Contents/Resources`; the privileged script
  compiled into the executable. `bundle.sh` fetches uv at a pinned version
  and checksum and takes the lock from the wheel it bundles.
- The bundle self-check before the prompt, the translocation check, and the
  Update, Roll back, Repair and Uninstall actions.
- `InstallProbe` stays as it is: the plists still come from `voiced
  install`, and the state directory, where the socket lives, is always
  `~/.voiced`.

## Alternatives considered

- **`SMAppService.daemon(plistName:)`.** *Measured* with
  `menubar/Prototypes/SMDaemonProbe` (a bundle with
  `Contents/Library/LaunchDaemons/<label>.plist` whose `BundleProgram` is a
  launcher inside the bundle):

  | Signature | `register()` | status afterwards | `unregister()` |
  |---|---|---|---|
  | ad hoc (`codesign -s -`) | throws `SMAppServiceErrorDomain` 1, "Operation not permitted" | `requiresApproval` | ok, `notRegistered` |
  | Apple Development, hardened runtime | same | `requiresApproval` | ok, `notRegistered` |
  | Apple Development, plist edited after signing | throws code 3, -67054 | `notFound` | n/a |
  | the edited bundle re-signed ad hoc | as the first row | `requiresApproval` | ok |

  Every registration was undone in the same run. The third row is not a
  barrier: -67054 only means the bundle changed after signing, and a local
  ad hoc re-sign registers again. The real reasons to reject it:

  - `UserName` must be the installing user, and the per-user environment
    (`HOME`, `VOICED_CONFIG`, `HF_HOME`, the state directory) cannot be in
    a plist sealed at build time. That leaves a root launcher in the
    bundle that reads the user and drops privileges, which is only safe
    if the bundle cannot be swapped, and a cask-installed bundle is
    user-writable (see the privileged step). Whether launchd re-checks the
    signing requirement at each launch is not documented and was not
    measured.
  - A development build signed ad hoc would then be a root program run at
    every boot.
  - A shared `UserName` or role account would not meet "runs as the
    installing user", and SMAppService agents do not start before login.
  - Whether launchd honours `UserName` for an SMAppService daemon at all
    is unverified: it needs someone to approve the probe in System
    Settings, then `unregister`. That run is worth doing only if this
    option is reconsidered, for example if a later macOS lets a bundled
    daemon take its `UserName` at registration.
- **A LaunchAgent instead** (no root at all). Rejected in ADR 0004: it
  does not run before login.
- **Requiring Homebrew** for ffmpeg and espeak-ng. Rejected: the owner wants
  the app to be the whole install, and the PyPI wheels cover both.
- **Bundling Python and the whole environment in the app.** About 750 MB
  for every app update. The binary wheels would all have to be re-signed
  and notarized as part of the app, GPL components would ship inside it,
  and the CLI-only install would differ from the app install.
- **Installing voiced from the GitHub source archive or from PyPI by
  name.** The first draft did. It needs hatchling at install time, has no
  hash, and the PyPI name needs a release. The bundled wheel needs neither.
- **Updating the environment in place** (reinstalling into the running
  venv). No rollback, and a failed install leaves the daemons on a half-updated
  environment. Side by side costs one more environment on disk, about
  680 MB of mostly cloned blocks, until the next update.
- **Swift doing all of the installing.** Plist rendering, the uninstall
  safety rules and model fetching would exist twice and drift apart.

## Consequences

- One install path for everyone who uses the app, and one layout for app,
  formula and CLI installs. The CLI stays for headless Macs and is what
  the app calls underneath.
- A first install downloads about 3.1 GB (measured: 290 s for the models,
  27 s for the rest), needs 4.4 GB free, and takes one administrator
  password; later updates usually take none.
- The app grows from about 1 MB to about 30 MB because of uv.
- The app's main executable carries a script that runs as root. It is
  reviewed like security code: hard-coded labels and paths, a key
  allowlist, plists rebuilt from checked values, no inherited environment.
- Each release builds the app, the voiced wheel and the lock from one
  commit in one job.
- One install per Mac. A second account sees who has it.

## Open items for the owner

- **Developer ID ($99 a year).** It blocks the cask and any download a
  non-technical user can open: Gatekeeper on macOS 15 blocks an
  unnotarized app. It does not block the implementation or development
  builds, which work ad hoc today. `menubar/Scripts/SIGNING.md` has the
  steps once the certificate exists.
- **PyPI: claim the `voiced` name now.** The app no longer needs it (it
  ships the wheel), but the formula's name-based path and anyone's
  `pip install voiced` do, and a squatted name is hard to get back.
- **Development override.** A user default (`installSource`) pointing at a
  local wheel or checkout, so an unpushed development build of the app can
  install the matching voiced. Agree on the name and that release builds
  ignore it.
- **One admin-prompt probe** during implementation: `BASH_ENV` set in the
  app's environment must not reach the root shell (see the privileged
  step).
- **"Background Items Added"** with `AssociatedBundleIdentifiers` set, and
  the firewall exception with a LAN client, need a real install; they come
  with the implementation.
