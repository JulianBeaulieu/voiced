# 0018: Install from the menu bar app

Date: 2026-10-15. Status: proposed.

## Context

Until now, installing voiced has meant a terminal: Homebrew for ffmpeg and
espeak-ng, `uv`, `voiced install --source ...`, then `sudo bash` on the
printed script (ADR 0006). The owner decided that people install the menu
bar app and the app installs the daemons. The Homebrew cask installs the
app, the app installs and manages the services, and the app is where
progress and errors are shown. The `voiced` CLI stays for headless and
advanced use and keeps working as it does today.

The app (ADR 0014) talks only to the watchdog's control socket and reads
`/Library/LaunchDaemons` to tell "not installed" from "not running". With
this decision it also has to put Python, the voiced environment, ffmpeg,
espeak-ng and the models onto a Mac that may have no Homebrew and no
terminal user, and get three LaunchDaemons approved. Those daemons run as
the installing user, never as root, and start at boot before anyone logs
in (ADR 0004).

This record is the design. Everything marked *measured* was run on an M1 Max
with macOS 15.7.4, uv 0.13.1 and Xcode 26.3, inside a temporary `HOME`,
uv cache, uv Python directory and Hugging Face cache. Nothing was written to
`~/.voiced`, `/Library` or the user's caches, and nothing ran with sudo.
The prototypes are in `menubar/Prototypes/` and are not part of the app.

## The privileged step

Three LaunchDaemon plists have to reach the system domain. That needs an
administrator once at install, again at uninstall, and again whenever a
plist's contents change. Upgrading the package, changing a setting and
`restart all` need no root (ADR 0012).

### (a) `SMAppService.daemon(plistName:)`: rejected for now

Measured with `menubar/Prototypes/SMDaemonProbe`: a bundle with
`Contents/Library/LaunchDaemons/<label>.plist` whose `BundleProgram` is a
small launcher inside the bundle and whose `ProgramArguments` exec a program
outside it.

| Signature | `register()` | status afterwards | `unregister()` |
|---|---|---|---|
| ad hoc (`codesign -s -`) | throws `SMAppServiceErrorDomain` 1, "Operation not permitted" | `requiresApproval` | ok, `notRegistered` |
| Apple Development certificate, hardened runtime | same | `requiresApproval` | ok, `notRegistered` |
| Apple Development, plist edited after signing | throws code 3, "Codesigning failure loading plist ... -67054" | `notFound` | n/a |

Every registration was undone in the same run. The final status was
`notRegistered` and `launchctl print system/<label>` found no service.

The first two rows are the documented first-run behaviour: the job waits
for the user to turn it on in System Settings > General > Login Items.
`launchctl` does not know the job until then. So registering does not need a
Developer ID. Whether an ad-hoc or development-signed daemon then *launches*
after approval, and whether launchd honours `UserName` for it, could not be
checked without a person approving it in System Settings. That stays open
(see the last section).

The third row is why this option loses. The plist is sealed into the bundle's
signature, and SMAppService refuses one that differs from what was signed.
`UserName` has to be the account of whoever installs voiced, and `HOME`,
`PATH`, `VOICED_CONFIG`, `HF_HOME` and the venv path all depend on that
account and its config. None of them can be in a plist written at build
time. The only way left is a job with no `UserName`, which runs as root: a
launcher in the bundle reads which user to run as, drops to that user and
execs `~/.voiced/bin/voiced`. That launcher is a root process for each
service. It also has to be trusted to drop privileges before it touches a
file the user can edit. The services would stop being plain user processes
that the user's own watchdog can `kickstart` (ADR 0012). ADR 0012 already
rejected a root watchdog for the same reason: root would run code from
`~/.voiced`. Two smaller costs: approval still means a trip to System
Settings plus an administrator prompt, and the daemons stop working if the
app is moved or deleted, because their program lives inside it.

### (b) One administrator prompt for a script in the bundle: chosen

The app runs the root half of the install through the standard
authorization dialog: `NSAppleScript` with `do shell script "..." with
administrator privileges` (Apple TN2065). The command it runs is a script
shipped inside the signed app bundle, not the staged script in
`~/.config/voiced/launchd/`. The user can edit that staged script, so
running it as root would let any process of theirs become root between
staging and running. A prototype is `menubar/Prototypes/PrivilegedInstall/install-daemons.sh`.
It takes the user name, the staging directory and the labels and does the
following:

1. Refuses a user with uid below 500, a user with no home, and labels or
   names outside `[A-Za-z0-9._-]`.
2. Copies each staged plist into `/Library/LaunchDaemons` as a root-owned
   temporary file.
3. Checks the copy, never the original: it lints the plist, then checks
   that `Label` matches, that `UserName` is the user, that there is no
   `GroupName`, `InitGroups` or `RootDirectory`, and that
   `ProgramArguments[0]` is under the user's home.
4. Only when every copy passes does it boot out, move into place and
   bootstrap, so a refusal leaves nothing half installed.

*Measured*, with the destination redirected to a temporary directory and
launchctl skipped: plists staged by today's `voiced install` pass. A staged
plist with `UserName root`, with `UserName` removed, or with
`ProgramArguments[0] = /bin/sh` is refused, and nothing is installed. User
`root` is refused.

This works unsigned, so development builds take the same path and need no
fallback. It keeps the `UserName` daemons that ADR 0004 measured, including
starting at boot before login. With FileVault the daemons start once the
disk is unlocked, as today. The prompt says what it is for (`with prompt
"voiced needs an administrator to install its background services."`).
`AuthorizationExecuteWithPrivileges` (deprecated since 10.7) is not used.

### (c) `SMJobBless` privileged helper: rejected

It is deprecated since macOS 13, needs a signed helper and a
signing-requirement handshake, and leaves a root process installed
permanently just to copy three files a few times in voiced's lifetime.

## Provisioning without a terminal

### Python and the environment: a bundled uv

*Measured*: the official `uv-aarch64-apple-darwin` 0.13.1 binary is 27.8 MB,
or 14.3 MB compressed, and links only system libraries. It is signed with a
Developer ID (team 2DC432GLL2). Inside the app it is re-signed with voiced's
identity and the hardened runtime, like any nested code. Its licence is
MIT or Apache-2.0, and the licence text ships in `Contents/Resources`.

A cold install with an empty uv cache, no Homebrew on `PATH`
(`/usr/bin:/bin:/usr/sbin:/sbin`), a fresh `HOME`, and `voiced[stt,tts]`
built from the GitHub source archive of `f7a8644`:

| Step | Time | Result |
|---|---|---|
| `uv python install 3.12` | 3 s | CPython 3.12.15 (python-build-standalone), 71 MB |
| `uv venv --managed-python --python 3.12` | 1 s | |
| `uv pip install "voiced[stt,tts] @ <archive URL>"` | 21 s | 83 distributions, venv 680 MB |
| total | 27 s wall | about 205 MB of wheels downloaded (sum of the chosen wheels' sizes on PyPI) |

The uv cache came to 706 MB. On APFS uv clones files from the cache, so the
cache and the venv share most of their blocks. The app keeps the cache so
upgrades are fast, and "Free up space" removes it. The largest wheels are
mlx-metal (45 MB), llvmlite (41), scipy (29), onnxruntime (21), transformers
(13) and numpy (12). All of them are binary wheels for arm64, and nothing
was compiled.

Decisions that follow from this:

- **uv's state belongs to the app.** The venv's `bin/python` is a symlink
  into uv's Python directory (measured). If the venv used the user's default
  `~/.local/share/uv/python`, a later `uv python uninstall` would break the
  daemons. So the app sets `UV_PYTHON_INSTALL_DIR` and `UV_CACHE_DIR` under
  `~/Library/Application Support/voiced/`, sets `UV_NO_CONFIG=1`, and passes
  `--managed-python` so that a Homebrew or system Python is never picked.
  Uninstall removes that directory together with the venv.
- **Install from a URL, never through git.** On a Mac without the Command
  Line Tools, `/usr/bin/git` is a stub that opens the "install developer
  tools" dialog, so `git+https://...` is out. Until the first PyPI release
  the app installs the GitHub source archive of the exact commit it was
  built from (as measured). After the release it installs `voiced==<app
  version>` from PyPI, the default in ADR 0006. In both cases the app and
  the services have one version (ADR 0014).
- **Reproducible environments.** The measured install resolved the
  dependencies fresh. The release build should instead ship a
  `requirements.lock` with hashes (`uv pip compile --generate-hashes`) in the
  bundle and install with `uv pip sync --require-hashes`. Then an app
  version always gets the dependency versions it was tested with.

### ffmpeg: a wheel, not Homebrew and not the bundle

voiced uses ffmpeg to decode uploads and to encode `mp3` (libmp3lame) and
`opus` (libopus) (ADR 0007). *Measured*: the `imageio-ffmpeg` 0.6.0 wheel
for macOS arm64 is 21.1 MB. It contains a static ffmpeg 7.1 (49 MB) that
links only system frameworks. It has `--enable-gpl --enable-libmp3lame
--enable-libopus` and both encoders. It installs into the venv from PyPI
like any other dependency. The Python package is BSD-2-Clause. The
binary inside it is a GPL build.

### espeak-ng: the copy that is already in the venv

`kokoro-onnx` already depends on `espeakng-loader`, so every voiced venv has
espeak-ng 1.52 (a 0.5 MB dylib and 21 MB of data). ADR 0009 avoided that copy
because espeak-ng breaks on long data paths. *Measured* with the engine's
own `_load_espeak` and the library from the wheel:

| `VOICED_ESPEAK_DATA` | length | result |
|---|---|---|
| copies under `~/.vqt/ppp.../espeak-ng-data` | 52 to 152 characters | correct phonemes |
| the same, 162 and 182 characters | | `Error processing file '/Users/runner/work/espeakng-loader/.../phontab'` (falls back to the compiled-in path) |
| a venv laid out like `~/.voiced/lib/python3.12/site-packages/espeakng_loader/espeak-ng-data` | 86 | correct |
| the scratch venv reached through a symlink | 83 | fails; not investigated further |

At the default `~/.voiced` the data path is 72 characters plus the length
of the user name, well under the limit of about 155. So with a real path, a
Mac needs no Homebrew espeak-ng. Homebrew's keg stays first in
the candidate list. The venv copy is the fallback, used only when its
resolved data path is under 150 characters. Otherwise `doctor` names the
problem.

### Licences

The app bundle contains only MIT code (voiced) and uv (MIT or Apache-2.0).
The GPL components, espeak-ng (GPL-3, through `espeakng-loader`) and the GPL
ffmpeg build (through `imageio-ffmpeg`), are downloaded from PyPI by the
user's own machine at install time. Today's install already downloads
`espeakng-loader` the same way. voiced does not distribute them, so
nothing changes for voiced's MIT licence. ADR 0009's reasoning, "loaded at
run time, installed separately", still applies. Shipping either one inside
the app or the cask would make the project a distributor of GPL object code,
with the duty to provide source code and the licence text for that exact
build. espeak-ng is also loaded into the Python process, not run as a
separate program, so "mere aggregation" is a weak argument for it. Neither
is bundled. This is the project's reading, not legal advice.

### Models

The defaults are `mlx-community/parakeet-tdt-0.6b-v3` and
`mlx-community/Kokoro-82M-bf16`. *Measured* by running the installer's
own model download script into an empty `HF_HOME`: Parakeet is one file
of 2508.5 MB. Kokoro comes to 355.3 MB in 56 files: the weights and the
voice packs, without the repository's sample WAVs. Together that is 2.86 GB,
and it took 290 s including loading both engines (unauthenticated Hugging
Face downloads on this connection). Model download is by far the longest
step of a first install, so it is the one that needs byte progress. The
installer already loads each engine once after downloading it, which proves
the weights work before the daemons start.

### What a first install costs

| | Download | On disk |
|---|---|---|
| app (with uv) | about 16 MB zipped | about 30 MB |
| Python | about 25 MB | 71 MB |
| voiced environment | about 205 MB | 680 MB, plus the uv cache that shares its blocks |
| models | 2.86 GB | 2.86 GB |

The welcome screen states these numbers, worked out from the bundled
lock file and the configured models rather than hard-coded.

## Flow

The app's existing states (ADR 0014) gain an installer in front of them.
"Not installed" now opens the installer instead of the guide link.

1. **Welcome.** One screen: what voiced is, what will be installed and
   where (`~/.voiced`, models in the Hugging Face cache, three background
   services running as *you*), the download size and the disk space, and
   whether the disk has room. One button: "Install".
2. **Environment** (no prompt): the bundled uv installs Python and the
   environment. Each uv step is shown with its own status. uv has no
   machine-readable progress, so the bar for this step is driven by elapsed
   time against the measured typical duration.
3. **Models** (no prompt): `~/.voiced/bin/voiced install --json-progress`
   writes the config, the log directory and the staged plists, and
   downloads the models with byte progress.
4. **Approval**: one administrator prompt runs the bundled privileged
   script (described above).
5. **Starting**: the app waits for the watchdog's control socket and its
   `hello` (up to two minutes, because model loading is slow on a cold
   disk), then switches to the normal menu. Optionally it offers "Open
   voiced at login" (see Permissions).

Each step can fail on its own, and the error shows the step, the last
lines of output and "Try again". Running a step again is safe: uv reuses
its cache, `voiced install` keeps an existing config, and the privileged
script replaces plists atomically. "Cancel" before approval leaves files in
the user's home that "Uninstall" removes, and never anything in `/Library`.

- **Update.** On launch the app compares its own version with the one the
  watchdog reports in `hello` (or with `~/.voiced/bin/voiced --version` when
  the watchdog is not running). If the services are older, it offers
  "Update voiced". The update runs steps 2 and 3, compares the newly staged
  plists byte for byte with `/Library/LaunchDaemons` (which anyone can
  read), and asks for an administrator only if they differ. Otherwise it
  runs `voiced restart all`, which needs no root. The app does not update
  itself. The cask (`brew upgrade`) or a new download does that.
- **Uninstall** ("Settings > About > Uninstall voiced…"): first one
  administrator prompt for the bundled uninstall script (bootout and
  remove, the same lines as `uninstall-daemons.sh`), then
  `voiced uninstall --yes`. Removing the models (with their size shown),
  the logs and the config are separate checkboxes, unticked by default,
  as in ADR 0006. The app's uv directory goes with the environment. The
  app itself is moved to the Trash by the user or by `brew uninstall`.
- **Repair** (shown when the plists are installed but the socket does not
  answer after the startup grace, or when the services report an
  environment error): runs the update path with
  `--reinstall`. The administrator prompt appears only if the plists
  differ or a label is not loaded.

### Where the logic lives, and the change to ADR 0014

Swift orchestrates and the Python side keeps the knowledge. The app runs
three kinds of child process: the bundled `uv` (steps 2 and update), the
venv's `voiced` CLI as the user (`install --json-progress`, `uninstall`,
`restart all`), and the bundled privileged scripts through one
authorization prompt. Paths, plist contents, the model list and every
safety rule from ADR 0006 stay in Python, so the CLI and the app cannot
install different things.

This deliberately amends ADR 0014. "The app never runs `launchctl`" becomes
"the app runs `launchctl` only inside its two signed, root-run install and
uninstall scripts". Control (status, settings, restarts) still goes only
through the control socket. The app still never edits the config file.

## Permissions

The app asks for a permission only when the step that needs it comes up,
and says why in one line. If it does not need something, it never asks.

| Permission | Needed? | When and why | What the user sees |
|---|---|---|---|
| Administrator authorization | Yes | Step 4, uninstall, and an update whose plists changed: writing to `/Library/LaunchDaemons` needs root. | Before the system dialog: "To start voiced at boot, macOS needs an administrator to install three background services. They run as you, not as root." |
| Background items (System Settings > Login Items > Allow in the Background) | Not asked; macOS shows it | macOS 13+ lists every LaunchDaemon here and posts a "Background Items Added" notification. The plists name the app in `AssociatedBundleIdentifiers` so the entry shows as voiced with its icon. If the user turns it off, launchd will not run the services. | After install: one line, "voiced appears in Login Items as a background item; leave it on." If the services are turned off there, the menu says so and offers "Open Login Items" (`SMAppService.openSystemSettingsLoginItems()`). |
| Open at login (`SMAppService.mainApp`) | Optional | Only the menu bar icon needs it. The services run without the app. | A checkbox on the Done screen, off by default, already in Settings > General. |
| Notifications | No | Status is in the menu bar icon. If "notify me when voiced stops" is added later, the app asks then, from that toggle. | Nothing |
| Microphone, camera, screen recording | No | Audio comes in over HTTP or Wyoming from satellites and Home Assistant; the app never captures. No usage-description keys go in `Info.plist`, so it cannot even ask. | Nothing |
| Accessibility, Full Disk Access, Automation | No | `do shell script` runs inside the app and sends no Apple Events to other apps. The app reads only `~/.voiced`, `~/Library/Logs/voiced`, `~/.config/voiced` and `/Library/LaunchDaemons`, none of which privacy protection covers. | Nothing |
| Local Network | No | The app connects only to its Unix socket. Local Network privacy does not apply to launchd daemons (TN3179), so the services' LAN listeners need no prompt either. | Nothing |
| Keychain | No | The API key is in the config file, which is mode 0600 (ADR 0013). | Nothing |

If the application firewall is on (as it is on the measuring Mac), it may
block incoming connections to the services' Python, which python-build-standalone
signs only ad hoc. This is not new: today's installs use an ad-hoc-signed
Python too. It needs checking with a LAN client during implementation.

## What changes

### Python side

- `voiced install --json-progress`: one JSON object per line on stdout:
  `{"event": "step", "step": "config|logdir|venv|models|staging", "state": "start|done|skipped"}`,
  `{"event": "download", "model": "<id>", "bytes": n, "total": n}` (from
  `huggingface_hub`'s `tqdm_class` hook, throttled to 4 a second),
  `{"event": "log", "line": "..."}` and finally `{"event": "done",
  "staging_dir": "...", "labels": {...}}` or `{"event": "error", "step":
  "...", "message": "..."}`. Human output goes to stderr.
- `voiced install --skip-venv`, so the app can create the environment with
  its own uv first and call the `voiced` in that environment.
  `--uv PATH` lets the CLI use the bundled uv for upgrades.
- `voiced install --app-bundle-id ID` adds `AssociatedBundleIdentifiers` to
  the plists. The privileged script allows that key.
- `voiced uninstall --json` (the list of what was removed) and
  `--remove-uv-state DIR`.
- `espeak_candidates()` falls back to the `espeakng-loader` copy (resolved
  path under 150 characters), and `doctor` reports which copy is in use.
- `ffmpeg_path()` falls back to `imageio_ffmpeg.get_ffmpeg_exe()`.
  `imageio-ffmpeg` becomes part of the `stt` extra (it decodes uploads).
- `voiced --version --json` for the update check without the watchdog.
- The sudo script printed by the CLI stays as it is for terminal users. Its
  checks move into the bundled script; the CLI could later run the same
  checks.

### App side

- An `Installer` module in `VoicedMenuBarUI` (a state machine over the
  steps above, testable with a fake runner the way `FakeControlServer` is
  used today) and the welcome, progress and done views.
- `Contents/Helpers/uv`, `Contents/Resources/install-daemons.sh`,
  `uninstall-daemons.sh`, `requirements.lock` and the uv licence.
  `bundle.sh` fetches uv at a pinned version and checksum.
- A version check against `hello`, and Update, Repair and Uninstall
  actions.
- `InstallProbe` stays as it is: the plists still come from `voiced install`.

## Alternatives considered

- **SMAppService daemons**, see (a). This could be revisited if a later
  macOS lets a bundled daemon take its `UserName` at registration.
- **A LaunchAgent instead** (no root at all). Rejected in ADR 0004: it
  does not run before login.
- **Requiring Homebrew** for ffmpeg and espeak-ng. Rejected: the owner wants
  the app to be the whole install, and the PyPI wheels cover both.
- **Bundling Python and the whole environment in the app.** About 750 MB
  for every app update. The binary wheels would all have to be re-signed
  and notarized as part of the app. GPL components would ship inside it.
  The CLI-only install would differ from the app install.
- **Swift doing all of the installing.** Plist rendering, the uninstall
  safety rules and model fetching would exist twice and drift apart.

## Consequences

- One install path for everyone who uses the app. The CLI stays for
  headless Macs and remains what the app calls underneath.
- A first install downloads about 3.1 GB (measured: 290 s for the models, 27 s for the rest) and takes one
  administrator password; later updates usually take none.
- The app grows from about 1 MB to about 30 MB because of uv.
- The app bundle has a second job, the install scripts that run as root.
  They are reviewed like security code: fixed paths, argument validation,
  and checks on the root-owned copy.
- Releases need the PyPI package (or the source archive until then) and the
  app built from the same commit, in one release job.

## Open items for the owner

- **Developer ID.** This Mac has Apple Development and Apple Distribution
  identities but no Developer ID Application certificate. Distribution
  outside the App Store needs a Developer ID and notarization
  (`menubar/Scripts/SIGNING.md`), and that is a prerequisite for the cask.
  The privileged step chosen here works without it, so development can
  start now.
- **PyPI release `v0.1.0`.** Until it exists, the app installs the GitHub
  source archive of its own commit. That works (measured), but it needs
  hatchling at install time and has no hashes. The release also lets
  `PUBLISHED` in `install.py` become true.
- **One approval run on a real install**, if (a) is ever reconsidered: run
  `menubar/Prototypes/SMDaemonProbe` with `register`, approve it in System
  Settings, and read the result file. It shows whether `UserName` is
  honoured and whether an ad-hoc or development signature can launch. Then
  `unregister`.
- **The firewall check** above, and what "Background Items Added" shows
  for the plists once `AssociatedBundleIdentifiers` is set. Both need a real
  install, so they come with the implementation.
