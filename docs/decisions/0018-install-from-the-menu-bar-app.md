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
microphone. The model for the experience is Handy (handy.computer), an
open-source local speech-to-text app with a menu bar icon, first-run model
downloads and a signed, notarized release; voiced takes its patterns, not
its code. The `voiced` CLI stays for headless and advanced use, and the
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

Homebrew, CLI and app installs share the layout that ADR 0017 specifies in
[its install-layout section](0017-homebrew-packaging.md#the-install-layout)
(`~/.voiced/bin/voiced`, `envs/<UTC timestamp>`, `install.json`,
`~/.voiced/python/`); this record only adds the uv cache in
`~/Library/Application Support/voiced/uv-cache/` (`UV_CACHE_DIR`).

## The privileged step

Three LaunchDaemon plists have to reach the system domain. That needs an
administrator once at install, again at uninstall, and again whenever a
plist's contents change. Upgrading the environment, changing a setting and
`restart all` need no root (ADR 0012).

### (a) One administrator prompt for a script in the signed executable: chosen

The app runs the root half of the install through the standard
authorization dialog: `do shell script ... with administrator privileges`
(Apple TN2065). That command is not deprecated and notarization does not
flag it. Internally it goes through `AuthorizationExecuteWithPrivileges`,
and the root `/bin/sh -c` it starts inherits its caller's environment:
`PATH`, a planted `BASH_ENV` and test variables all reached it when
measured. That shell (bash 3.2) also reads `SHELLOPTS` and `PS4` before
running anything, so `SHELLOPTS=xtrace` with a command substitution in
`PS4` runs that command as root before `env -i` below, and
`launchctl setenv` can plant both for every app the user starts.
So the caller is never the app: the app runs `/usr/bin/osascript` as a
child with an environment of `PATH=/usr/bin:/bin:/usr/sbin:/sbin` alone,
and that `osascript` runs the `do shell script`. The command it runs is
always

```sh
/usr/bin/env -i PATH=/usr/bin:/bin:/usr/sbin:/sbin \
    /bin/bash --noprofile --norc -c "$SCRIPT" voiced-privileged <mode> <user>
```

with every tool inside the script called by its absolute path. The
AppleScript source is fixed; the script text, the mode, the user name and
the prompt reach it as `osascript` arguments (`on run argv`) and through
`quoted form of`, never spliced into the source.

`$SCRIPT` is compiled into the app's main executable
(`src/voiced/launchd/voiced-privileged.sh`, embedded with SwiftPM's
`.embedInCode`; see "One script for the app and the CLI"), not a file in
the bundle: a cask install puts the app in `/Applications`
owned by the user, so any process of that user could edit a script file
inside it. That the copy which runs is the signed one depends on the
hardened runtime without the `allow-dyld-environment-variables` and
`disable-library-validation` entitlements, so nothing can be injected into
the running app; that is a release requirement. Before showing the prompt,
the app checks its own bundle with `SecStaticCodeCheckValidity` (strict
validation, all architectures, and the designated requirement with
voiced's team identifier). This catches accidental damage, such as a
half-copied or partly modified bundle, and nothing more: an app someone
tampered with on purpose simply skips its own check. Debug builds skip it.
Until releases are signed with a Developer ID, the check runs without the
designated requirement: an ad hoc build has no team identifier to
require. The requirement is added with signing (see Signing and release
CI).

This narrows the risk; it does not remove it. A process running as the
user can still replace the whole app with its own and show the same dialog,
or show a look-alike dialog of its own. That is the same-user attacker ADR
0016 accepted: one who can already run code as the user and wait for them
to type a password. What the design guarantees is that root never parses
or runs anything a user process wrote.

The script is `src/voiced/launchd/voiced-privileged.sh`:

```
voiced-privileged install   USER    render the three plists and bootstrap them
voiced-privileged uninstall USER    boot out and remove them, if they are USER's
voiced-privileged check     USER    installed plists match and are loaded? (no root needed)
```

1. **The only input is the user name.** Root renders the plists itself from
   fixed values: the labels `io.github.julianbeaulieu.voiced.{stt,tts,watchdog}`,
   `ProgramArguments` `<home>/.voiced/bin/voiced` plus `watchdog` or
   `serve <service>`, `UserName`, `RunAtLoad`, `KeepAlive` exactly
   `{PathState: {<home>/.voiced/bin/voiced: true}}` (ADR 0017: launchd
   follows the symlink and stops respawning while it dangles),
   `ThrottleInterval` 10, `ProcessType Interactive`,
   `WorkingDirectory` the home, `AssociatedBundleIdentifiers` the app, and
   a fixed environment: `HOME`, `PATH` (`<home>/.voiced/bin` first),
   `VOICED_CONFIG` (`<home>/.config/voiced/config.toml`),
   `PYTHONUNBUFFERED` and `VOICED_STDIO_LOG`
   (`<home>/Library/Logs/voiced/<service>`). No plist the user wrote is
   parsed as root, so there is no allowlist to get wrong. The CLI renders
   no plist of its own for these daemons; a test runs the script and
   compares what it writes with what the CLI expects: the labels it
   addresses, the entry point it links, the config path it writes and the
   environment the Metal probe gets too.
2. **No `StandardOutPath` or `StandardErrorPath`**, as `voiced install`
   already writes them (ADR 0006): launchd opens those files as root, and
   `~/Library/Logs/voiced` belongs to the user, so a symlink planted there
   would be a root write anywhere. `VOICED_STDIO_LOG` tells each service to
   open its own log (`O_NOFOLLOW`, then `dup2` onto stdout and stderr).
3. Settings that used to travel in the plist environment (`HF_HOME`,
   `VOICED_ESPEAK_*`) come from the config file, which the services read
   at start (`[install] hf_home`); so does a custom `log_dir`, which then
   wins over the default `VOICED_STDIO_LOG`. A custom state directory,
   config path or `[launchd] label_prefix` is supported neither by the app
   (see Lifecycle) nor by the CLI's root step: `voiced install` then says
   so and prints no `sudo` line.
4. The user must exist and be a regular account: a uid from 500 to
   2147483647, which excludes `root`, system accounts and `nobody`
   (4294967294). The name is replaced by the account's own spelling
   (`id -un` of the uid), since Directory Services ignores case. The home
   comes from `dscl -plist`, so spaces and quotes work, is converted to NFC
   (Directory Services may return it decomposed), and must be owned by
   that uid. Uninstall does not need the home at all.
5. **One install per Mac**, compared by uid: if a
   `/Library/LaunchDaemons/<label>.plist` belongs to another account, both
   modes refuse, and the app says "voiced is already installed for <user>
   on this Mac." An install whose `UserName` no longer resolves (the
   account was deleted) is orphaned: install and uninstall take it over and
   say so. Two runs at once are stopped by a kernel lock (`flock` on a file
   descriptor the shell keeps open, taken through `/usr/bin/perl`, since
   macOS 14 has no `/usr/bin/lockf`) on `/var/run/voiced-privileged.lock`,
   held until the script exits however it exits, so there is no stale lock
   and no pid to be reused. A lock that cannot be taken at all is reported
   as that, not as another run.
6. Temporary files go to a root-owned 0700 directory under `/private/tmp`
   (never `$TMPDIR`) and are removed by a trap on any exit.
7. Only after all three plists are rendered does it touch launchd: per
   label, boot out, wait until the label is gone, move into place,
   bootstrap. A bootstrap that fails is reported per label (exit 3) and the
   others stay as they are; the app offers Repair. A refusal writes nothing
   to `/Library`; a launchd failure halfway can leave a label booted out,
   which is why the result is per label.
8. When the application firewall is on, it allows the services' Python
   (see Permissions). After a successful bootstrap, a failure there is a
   warning (exit 4), not a refusal, and a Python deliberately left alone
   because it is outside the home is exit 5, so the app can say which.

*Measured* with `menubar/Scripts/test-privileged.sh`, run as a
normal user against a scratch destination with launchctl and the firewall
skipped (the override is ignored when the script runs as root): the
rendered plists are this account's (label, `UserName`, `~/.voiced/bin/voiced`,
the bundle identifier, mode 0644; pytest compares every key with what the
CLI expects, ADR 0017's `KeepAlive` and no log keys included);
`check` reports them up to date and notices a changed one. Refused with
nothing written: user `root`, a uid below 500, `nobody`, an unknown user,
a malformed name, extra arguments, a home owned by another account, a held
lock. Another account's install is neither replaced nor removed; an
orphaned one is taken over and reported; a lock file nobody holds does not
block; a user name in the wrong case installs under the account's
spelling; uninstall works without the home; a home with spaces, quotes, a
backslash and non-ASCII characters renders exactly. With a stand-in
`socketfilterfw`, a Python under a home reached through a symlink is
allowed, and one outside the home is left alone with exit 5. 26 of 26
cases pass.

#### One script for the app and the CLI

The CLI used to stage the three plists in `~/.config/voiced/launchd/` and
print a generated `install-daemons.sh` that copied them into
`/Library/LaunchDaemons`. Those files were the user's: a process running as
the user could rewrite a staged plist after the user had read the script
(drop `UserName`, make `ProgramArguments` `/bin/sh -c ...`) and get code run
as root at every boot. Its `uninstall-daemons.sh` had no owner check and no
lock, so it removed another account's daemons, and both used bare tool
names and no `umask`. The two root scripts had already drifted.

So there is one: `voiced install` stages nothing for root and prints

```sh
sudo /bin/bash <active environment>/lib/python3.12/site-packages/voiced/launchd/voiced-privileged.sh install <user>
```

and `voiced uninstall` the same with `uninstall`; the dry run shows the
line too. The path is the copy in the environment `~/.voiced/bin/voiced`
points at, or the running package's when that environment has none of its
own (an editable checkout), fully resolved: for Homebrew it names the
versioned keg, so it is for running now, and `voiced uninstall` prints a
fresh one later. Everything above holds for the CLI: root renders the plists from
the user name, refuses system accounts and another account's install,
takes the lock, and runs every tool by path; sudo resets the environment
before bash starts. The file itself sits in an environment the user's
account can write. That is the trust of any script a user chooses to run
with `sudo`, and the same as the generated script before it; the docs say
so and invite reading it first. No other account may share it: the CLI
prints the line only when the script and every directory on its resolved
path belong to the user or root and nobody else may write them, by mode
bits or by an ACL allow entry granting a write, add or delete to anyone but
the user or root. A group-writable directory passes only if its group holds
no other account; `_mbsetupuser`, the setup assistant's account, is left
out, because it is in `admin`, the group of Homebrew's directories, on
every Mac, and on a stock Mac it is the only account other than root below
uid 500 in `wheel`, `staff` or `admin`. A sticky directory passes on its
mode bits. Checking the resolved path and printing it, rather than walking
each link of a chain such as Homebrew's `opt`, is the simpler of the two
and complete: the line root runs has no link left to swap. Otherwise, for
example a Homebrew that another admin owns, it prints why and points to
`voiced install --source`, which builds an environment of the user's own.
A volume mounted with ownership ignored shows every file as the user's, so
the check cannot judge an environment on one. What
changes is that nothing else the user's account wrote is read as root. The
Metal probe of ADR 0004 follows the same rule: its plist is inside the
script `voiced doctor --metal-probe` writes, its label uses the fixed
prefix whatever the config says, and its result file is removed and read
as the user.
`voiced install` deletes the files earlier releases staged, so a stale
`install-daemons.sh` cannot be run by mistake.

The script lives where both consumers can take it, `src/voiced/launchd/`,
so the wheel ships it as package data without a build hook, and an
editable checkout runs it in place. The menu bar target embeds
`Installer/voiced-privileged.sh`, a relative symlink to that file.
*Measured* with SwiftPM 6 (Xcode 26.3): `.embedInCode` follows the link;
the generated `PackageResources.voiced_privileged_sh` holds exactly the
file's bytes, an edit of the target file rebuilds the embedded
copy incrementally, and `swift build -c release` and `bundle.sh` are
unchanged. The wheel and sdist built by hatchling contain the file. The
reverse (canonical in `menubar/`, force-included into the wheel) would
need a `force-include` entry, a repo-relative fallback for editable
installs, and would put a Python runtime file under the Swift tree; a copy
kept equal by a test is a second file to edit. Guards: a Swift test
compares the compiled-in bytes with the packaged file, a pytest checks the
symlink is relative and resolves to it, and `test-privileged.sh` runs the
script through the symlink.

What the root step installs is fixed: `~/.voiced/bin/voiced`,
`~/.config/voiced/config.toml`, the default labels. The settings that once
reached the plists are read from the config file at start (item 3), so
only three options remain that the root step does not follow: a state
directory other than `~/.voiced` (`--state-dir`, `[install] venv_dir`), a
config path other than the default (`VOICED_CONFIG`), and `[launchd]
label_prefix`. `voiced install` with any of them still does the user-level
install, for test harnesses and `voiced serve`, and says why it prints no
root step; `voiced uninstall` likewise prints none, so a harness's
`uninstall --state-dir` cannot be handed the line that removes the real
daemons. `label_prefix` stays a runtime key: it names the labels that
`status`, `start`, `stop`, `restart`, `doctor` and the watchdog address, so
a harness that loads its own jobs under another prefix keeps working, and
an install an earlier release made under one is still controlled.
`voiced uninstall` points such an install out, since the script removes
the default labels only; operations.md has the two commands per label.
Nothing in this repository loads jobs under another prefix.

The prompt says what it is for (`with prompt "voiced needs an
administrator to install its background services."`). Cancelling it
(error -128) is not an error: the app goes back to the "Install background
services" screen. This path works unsigned, so development builds take it
too. It keeps the `UserName` daemons that ADR 0004 measured, including
starting at boot before login; with FileVault they start once the disk is
unlocked, as today.

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
  it. (Changed 2026-10-23: the models are the `catalogue`, every catalogued
  model with its pin and size, the defaults marked recommended; see
  "Amendment (2026-10-23)".)

### Python and the environment

| Step | Command (bundled uv, `UV_NO_CONFIG=1`, `UV_LINK_MODE=clone`) |
|---|---|
| Python | `uv python install --no-bin 3.12.15` into `~/.voiced/python`. `--no-bin` because without it uv wrote `~/.local/bin/python3.12` onto the user's `PATH` (measured). The patch version, because the `3.12` minor link moves silently on the next install |
| Environment | `uv venv --relocatable --managed-python --python 3.12.15 ~/.voiced/envs/<id>.partial`, renamed to `envs/<id>` when the packages are in; `<id>` is a UTC timestamp, as ADR 0017 names environments (2026-10-20: `-2`, `-3` on a collision, and names `install.json` holds are skipped; ADR 0017, 2026-10-20 note). `--relocatable` because of the rename: without it every console script uv writes, `bin/voiced` included, starts the absolute path `envs/<id>.partial/bin/python` and fails once renamed (measured with uv 0.9.28); with it they find `python` next to their own resolved path, which also holds through the `~/.voiced/bin/voiced` symlink |
| Packages | `uv pip install --require-hashes --no-deps -r requirements.lock`, then `uv pip install --no-deps <voiced wheel>` (a built wheel, so ADR 0017's `build-requirements.lock` has nothing to constrain) |

*Measured* cold, with an empty uv cache, no Homebrew on `PATH`
(`/usr/bin:/bin:/usr/sbin:/sbin`) and a fresh `HOME`: Python 3 s (71 MB),
venv 1 s, packages 21 s (83 distributions, venv 680 MB, about 205 MB of
wheels). 27 s wall clock. All wheels are arm64 binaries; nothing compiles.
The largest are mlx-metal (45 MB), llvmlite (41), scipy (29), onnxruntime
(21), transformers (13) and numpy (12). On APFS uv clones files from its
cache, so cache and venv share most blocks.

`voiced install --source` already uses the same uv layout under ADR 0017:
`UV_PYTHON_INSTALL_DIR` under the state directory and `--managed-python`, so
a CLI install's Python can no longer be removed by an unrelated `uv python
uninstall` (the root cause behind ADR 0016 finding 15), and it installs from
the lock like every other path. This record adds `UV_NO_CONFIG=1` there, so
a user's `uv.toml` cannot change what either path installs.

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

Done since (`voiced.ffmpeg_exe`): `doctor` also names where the binary
comes from (`imageio-ffmpeg 0.6.0`, `PATH`, or imageio-ffmpeg's own
`IMAGEIO_FFMPEG_EXE` override). The licence of the binary is
GPL-2.0-or-later (`ffmpeg -L`; configured with `--enable-gpl`, without
`--enable-version3` or `--enable-nonfree`); the Python package is
BSD-2-Clause. *Measured*: ffmpeg 7.1 probes raw PCM on a pipe before it
encodes, so the first mp3 or opus bytes of a streamed reply came about
1.2 s late (Homebrew's ffmpeg 9 does not probe raw input); with
`-probesize 32` on the encoder's input both start within 30 ms. The
disconnect test, which needs the first bytes before synthesis ends, caught
it.

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
Done since (ADR 0009, `kokoro._espeak_data`): at each load the engine clones `espeak-ng-data` with
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

Changed 2026-10-20 (`kokoro.espeak_candidates`): until now only the data
half of this was in the code: `espeak_candidates()` listed the override or
the Homebrew kegs and never the wheel's library, so the 0.1.0 cask and DMG
installs, which do not install Homebrew's espeak-ng, failed text-to-speech
at load with `EspeakNotFound` on a Mac without the keg. The code now
matches this section. `espeak_candidates()` is the one place that sets the
order and returns `EspeakCandidate`s with their source: the override alone
when set, else the wheel's library (`espeakng_loader.get_library_path()`)
with its data cloned to the short path, then the kegs. A candidate whose
data path would be too long or cannot be cloned is skipped for the next
one rather than ending the load. A clone counts as complete only when it
has `phontab`, `phondata`, `phonindex`, `intonations` and `en_dict`, since
espeak-ng exits the process on a missing one; otherwise it is made again,
one load at a time under a lock file. A library that does not load is
skipped before the tokenizer, which would otherwise quietly run a
system-wide espeak-ng.
`voiced doctor` uses the same data path check (`espeak_data_path`, which
copies nothing) and names the first copy load would accept: the wheel's,
the keg or the override. The cask needs no espeak-ng dependency; the
formula's `depends_on "espeak-ng"` is now only the fallback.

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

Done since (`voiced.models`):

- `voiced.models.PINS` is the one place a default model's commit and size
  are set. The engines load that revision too (Parakeet from the pinned
  snapshot directory, Kokoro through `mlx-audio`'s `revision=`), from the
  cache without a network call once it is filled. A model that is not
  pinned, one the config names, loads `main` as before.
  `python -m voiced.models` prints `{"models": [{"repo", "revision",
  "bytes"}]}` with the standard library alone, for the bundle script's
  `install-manifest.json` (changed 2026-10-23: it prints the `catalogue`
  instead; see "Amendment (2026-10-23)"); `bytes` is the size of what
  `download_patterns()` names at that revision (Parakeet `config.json` and
  `model.safetensors`, 2,508,532,829 bytes; Kokoro `config.json` and every
  `*.safetensors`, the 54 voices included, 355,322,783 bytes), checked
  against the hub by a `model` test.
- `download_patterns()` is an optional classmethod, like
  `catalogue_voices`, not part of the frozen interface: an engine without
  it (the fakes, a plugin, Soprano) downloads in its own `load()`.
- The models step runs `python -P -m voiced.models download --config FILE`
  in the environment being installed, `--report` with `--json-progress`. It
  writes its own lines with the prefix `@@voiced-download `, each with a
  `kind`: first `model` with the model, revision and Hugging Face cache it
  fetches, then `bytes` with `{"repo", "bytes", "total"}` at most four
  times a second. A dry run of
  `snapshot_download` first finds what is cached, so a resumed download
  starts at the bytes already there. The bytes counted are those of
  `snapshot_download`'s bar of bytes written to disk, through its
  `tqdm_class`.

### Licences

The app bundle contains only MIT code (voiced, its wheel) and uv (MIT or
Apache-2.0). The following are installed from PyPI by the user's own
machine at install time, are not part of voiced's MIT code and are never
in the app, the cask or a release asset:

| Component | Licence | Comes from |
|---|---|---|
| phonemizer | GPL-3.0-or-later, imported into the TTS process | PyPI |
| espeak-ng 1.52 | GPL-3.0-or-later; the wheel carries no licence text | `espeakng-loader` |
| ffmpeg 7.1, GPL build with x264, x265, LAME and Opus | GPL-2.0-or-later | `imageio-ffmpeg` |
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
"Not installed" opens the installer instead of the guide link. Each screen
says what it does in one sentence. There is no model picker on first run:
each engine gets its pinned default, and the model is chosen later in the
Speech to Text and Text to Speech panes. (Changed 2026-10-23: the welcome
screen is now a setup with a model per service; see "Amendment
(2026-10-23)".)

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
2. **Environment** (no prompt): holding ADR 0017's install lock (an
   exclusive `flock` on `~/.voiced`; installs never prune an
   `envs/<id>.partial`), the app first claims `~/.voiced`, the way
   `voiced install` does, before it builds anything there:

   - missing, empty, or holding only `control.sock` or `.DS_Store`: create
     it if needed, write exactly

     ```json
     {"source": "app", "environment": null}
     ```

     to `~/.voiced/.install.json.tmp` and rename that to
     `~/.voiced/install.json` (the other manifest fields default to
     empty);
   - `install.json` exists, or the directory is a legacy venv as ADR 0017
     defines it (`pyvenv.cfg`, and a voiced `.dist-info` without
     `"editable": true`), which `voiced install` adopts and replaces: leave
     it as it is;
   - a symlink, or anything else: refuse (naming the link's target or the
     entries), as `voiced install` would, and build nothing.

   Then the steps in the table above, until the environment is renamed,
   then releasing it for step 3, which takes it itself. A CLI install that
   gets the lock in that moment could prune the renamed environment; step 3
   then fails its engine check and "Try again" builds it again. uv has no
   machine-readable progress, so this step shows an indeterminate spinner
   with the current step's name, not an invented bar. It takes about 27 s.
3. **Models** (no prompt):
   `~/.voiced/envs/<id>/bin/voiced install --origin app --json-progress`
   runs ADR 0017's install steps (`install_steps`, under its install
   lock): writes the config if absent, creates the log directory and claims
   `~/.voiced`, checks the environment imports the engines, checks the
   configured ports are free (added here), downloads the models with byte
   progress and loads each engine once, and only then links
   `~/.voiced/bin/voiced` to its own environment, writes `install.json`
   (`--origin app` makes its `source` `app`) and prunes old environments.
   It stages nothing: the next step renders the plists as root.
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

### On every launch

Installing once is not enough: the user can turn the background item off,
a plist can change, and the app can be updated under the services. So each
launch of an installed app re-checks, without root:

- the background item's approval (`SMAppService` status for the app's
  `AssociatedBundleIdentifiers` entry, polled while the card is shown): if
  it is off, a card says "voiced can't start at boot while it is off in
  Login Items" with "Open Login Items". The app asks with
  `SMAppService.statusForLegacyPlist(at:)` for each installed plist, off
  the main thread, and takes `requiresApproval` as off; that this is what
  macOS reports for a switched-off item is an open item below;
- the plists, with `voiced-privileged check`: a difference offers Repair;
- the `hello` version and lock hash: a mismatch offers "Update voiced" (for
  `source = app`) or shows it (other sources).

### In the menu bar

The icon has three states, each drawn for light and dark: idle (running),
busy (starting or installing), and an error badge. The menu ends with a
disabled line showing the app's version. A Debug pane in Settings (log
locations, paths, the environment in use, "Copy diagnostics") is hidden
until a "Show Debug pane" switch in General turns it on.

After a successful "Update voiced", a "What's new" window shows the release
notes bundled with the app (Markdown), once.

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
| Port in use | `voiced install` checks the configured ports before approval and names the process holding one; a port held by voiced's own running services (a reinstall or update) is not a conflict |
| Download interrupted | "Try again" resumes from the `.incomplete` files |
| Download goes wrong | Progress shows speed and time remaining and is capped at the manifest's size; more bytes than that is an error, and that file's `.incomplete` is deleted and the file restarted. No bytes for 60 s counts as a stall and offers "Try again". The error names the manual fallback: the repository, revision and target folder, with "Show in Finder" for the models folder |
| App quit mid-install | The app terminates its children (SIGTERM, then SIGKILL after 5 s). The next launch reads progress from disk (a `.partial` environment is deleted, a complete one in `envs/` is kept, cached models count as done, installed plists mean only Starting is left) and offers "Continue" |
| One label failed to bootstrap | Names the service; offers Repair |
| Homebrew is working on voiced (`source = homebrew`) | The watchdog refuses a requested restart while Homebrew holds the formula's lock (`service_unavailable`, ADR 0017); the app says "Homebrew is updating voiced" and tries again when it ends |
| Another user's install | "voiced is already installed for <user> on this Mac." Nothing else is offered |

### The progress protocol

`voiced install --json-progress` writes one JSON object per line on stdout;
human output, including the output of the commands the steps run, goes to
stderr (`voiced.launchd.progress`).

- The first line is `{"event": "hello", "protocol": 1, "version": "<voiced version>"}`.
  The app refuses any other protocol number with "Update voiced" or
  "Update the app", whichever side is older. Unknown events and unknown
  fields are ignored, so adding either keeps protocol 1.
- `{"event": "step", "step": "<id>", "state": "start|done", "shown": "..."}`
  for each of ADR 0017's `install_steps`, by their `Step.id` (`config`,
  `claim`, `check`, `ports`, `models`, `link`, `manifest`, `legacy`,
  `prune`, and `unstage` when an earlier release left staged plists; a
  `--source` build adds three `build` steps, which the
  app never runs); the app's own environment step is `env`. A step that
  does not apply is not in the list. The same list drives `--dry-run`, so
  `--dry-run --json-progress` gives the app the steps before it starts:
  one `step` event per step with `"state": "planned"`, then `done`.
  `shown` is the step's description without the dry run's "would" or a
  command's `$ `.
- `{"event": "download", "model": "<repo>", "bytes": n, "total": n}`, with
  the total from the manifest, throttled to four a second. It arrives with
  the model download that reports bytes (`snapshot_download` with pinned
  revisions); until then the app shows the models step without a byte
  count. The child reports on lines with an explicit prefix of its own
  (`@@voiced-download `), not one that merely looks like this event; those
  lines are not logged.
- `{"event": "log", "line": "..."}`: each line the steps' child processes
  print (uv, the model download), as it comes; of a progress bar's `\r`
  redraws, only the last state of the line.
- The last line is `{"event": "done"}` or
  `{"event": "error", "step": "...", "message": "..."}`, and the exit code
  agrees with it (0 only with `done`). `step` is the step that was running,
  or null for a refusal before any step (another account's state
  directory, root, a config file that does not parse). A `models` error
  also has `repo`, `revision` (null when not pinned) and `path`, the
  Hugging Face cache directory, when known (the download had named the
  model it was on), for the manual fallback and Show in Finder.
  An unexpected
  exception still ends the stream with `error`, then exits non-zero with
  its traceback on stderr. SIGTERM (the app's Cancel) kills the running
  step's child process, ends the stream with `error` ("terminated
  (SIGTERM)") and exits 143.

The watchdog's control-socket `hello` (ADR 0013) gains `install`: what
`install.json` records (`source`, `environment`, `version`, `lock_sha256`,
`python`), read for each connection, or null when there is none.

## Lifecycle

### Updates, with rollback

The app compares its own pins with the running services. The watchdog's
`hello` reports what `install.json` records (`source`, `version`,
`lock_sha256`, `python`; added fields, so protocol 1 still applies), and
without a watchdog the app reads `~/.voiced/install.json` itself. Any
difference, in either direction, is a mismatch. For `source = app` the
app's lock wins: an older app on a newer environment offers to install its
own pins too, so app and services are one version (ADR 0014). For any other
source the app shows the mismatch and does not act on it (see below).

"Update voiced" then:

1. builds a new environment beside the old one (`envs/<id>.partial`,
   renamed when its packages are in);
2. runs the new environment's `voiced install --origin app --json-progress`,
   which checks it (ADR 0017's engine check, which also reports its
   version), downloads any changed model revision, and only then repoints
   `~/.voiced/bin/voiced`, rewrites `install.json` and prunes all but the
   new and the previous environment;
3. runs `voiced-privileged check` as the user (no root: the plists in
   `/Library/LaunchDaemons` are world-readable and `launchctl print
   system/<label>` needs no root, measured), and `voiced-privileged
   install` with its prompt only if a plist differs or a label is not
   loaded. When the firewall is on and the resolved Python changed (a new
   Python version), that also counts as a difference, so the exception
   follows it;
4. waits for a `hello` with the new lock hash. The watchdog restarts the
   services and itself when the symlink changes (ADR 0017); if no such
   `hello` arrives within the startup grace, the two minutes of step 5 of
   the Flow, the app runs `voiced restart all` and waits once more.

If step 4 fails, "Roll back" runs the previous environment's own
`voiced install --origin app --json-progress`, which points the symlink
back at it (models are cached, so it is quick). `install.json` names that
environment in `previous` (ADR 0017); the app rolls back only to one inside
`envs/` that still has its `bin/voiced`, and Settings > About offers the
same "Roll back" afterwards. A failure in step 2 before it links the new
environment has not touched what runs, so it offers only "Try again". Pruning in step 2 keeps
the previous environment for one-click rollback; after a successful update
the app also removes Python versions no remaining environment uses, and clears the
uv cache: the cache only grows (706 MB to 928 MB after one update,
measured) and `uv cache prune` finds nothing to remove, while the next
update re-downloads only what changed.

### An install that already exists

The app reads `~/.voiced/install.json` and the installed plists in
`/Library/LaunchDaemons` (whose `UserName` says whose install it is), and
follows ADR 0017's adopt-or-refuse rules. "Controls" means status, settings
and restarts through the socket; "manages" means the app also updates,
repairs and rolls back the environment.

| Found | The app |
|---|---|
| plists for another account | refuses (one install per Mac) |
| `source = app` | manages it: the normal case |
| `source = homebrew` | controls it; a version mismatch is shown, with "Updated by Homebrew: `brew upgrade voiced`". "Switch to updates from the app" builds an app environment and runs its `voiced install`, which repoints the symlink; config and models stay, and the keg is untouched until the user runs `brew uninstall voiced` |
| `source = cli` (a `--source` environment in `envs/`) | the same: controls it, shows a mismatch, and switches only when asked. If that environment's voiced was installed from a local checkout (an editable or `file://` install in `direct_url.json`), the app says so before switching |
| a legacy install (`~/.voiced` itself is a venv) | adopts it as `environment = ~/.voiced` and controls it as is. The first switch has no rollback: the legacy venv's own `bin/voiced` is the entry point path, so the two cannot coexist. The new environment is built first; `voiced install` removes the legacy venv's files once that environment passes ADR 0017's engine check and is linked, before the app waits for its `hello`. If the legacy plists ran a custom `venv_dir`, that switch's privileged step replaces them with the fixed ones |
| plists with a custom `[launchd] label_prefix` | controls it (ADR 0014 finds it whatever the prefix); does not manage it. The first switch to the app's environment boots out the custom labels and installs the default ones, in one prompt |
| no plists installed, environment present | continues at Approval |

Apart from the legacy case, switching never needs root (the plists do not
change) and never deletes the previous environment on the spot; it becomes
the rollback target and is pruned on the next update.

### Repair

Shown when the plists are installed but the socket does not answer after
the startup grace, when the services report an environment error, or after
a failed bootstrap.

- For `source = app`: uv's `pip install --reinstall --require-hashes
  --no-deps -r` of the bundled lock and uv's `pip install --reinstall
  --no-deps` of the bundled wheel into the active environment, then that
  environment's `voiced install --json-progress`. If the environment's
  Python is gone, the broken environment directory is moved aside
  (`envs/<id>.broken`) and a fresh one is built, exactly as an update does.
- For any other source the app does not touch the environment. It offers
  the switch to a fresh app environment, or shows that source's own fix
  (`brew reinstall voiced`, or `voiced install --source ...` again).

The administrator prompt appears only if `check` reports a difference.

### Uninstall and removing the app

"Settings > About > Uninstall voiced…" runs `voiced-privileged uninstall`
(one prompt), then `voiced uninstall --yes`. Models (with their size), logs
and config are separate checkboxes, unticked by default, as in ADR 0006.
The privileged uninstall does not need a working environment: it takes the
labels from the hard-coded list, removes only plists that belong to this
account, and deletes nothing else. `voiced uninstall` deletes only what
`install.json` and ADR 0006's rules say is voiced's (environments only if
they contain `pyvenv.cfg`, and `~/.voiced/python`); a Homebrew keg is never
touched. If the active environment is too broken to run `voiced
uninstall`, the app offers Repair first and Uninstall after it.

Moving voiced.app to the Trash leaves voiced running; the daemons do not
need the app. The welcome text and the README say: use Uninstall in
Settings first. Without the app, `voiced uninstall` and the `sudo` line it
prints, the same script, still work from a terminal.

The cask adds:

```ruby
uninstall quit: "io.github.julianbeaulieu.voiced.menubar"
zap launchctl: ["io.github.julianbeaulieu.voiced.stt",
                "io.github.julianbeaulieu.voiced.tts",
                "io.github.julianbeaulieu.voiced.watchdog"],
    trash:     "~/Library/Application Support/voiced"
```

so `brew uninstall --cask voiced` removes the app and leaves the daemons
running, as the Trash does, and `--zap` stops and removes them (with their
plists) and the uv cache. Homebrew runs `uninstall` on upgrade and
reinstall too, with `sudo`; removing the daemons there cost every update a
second prompt (ADR 0017, "Amendment (2026-10-23)"). Neither `uninstall`
nor `--zap` touches `~/.voiced`, the config or the models; the cask's
caveats say so.

### Updating the app itself

Through the cask only (`brew upgrade --cask voiced`), or a new download
from the GitHub release. The app does not update itself, and there is no
Sparkle: one more signing key and feed for a tool whose users mostly come
through Homebrew is not worth it yet. So the cask has no `auto_updates`
stanza, and `brew upgrade` handles it. A new app version finds the mismatch
above on its next launch and offers "Update voiced".

### Signing and release CI

One manually started workflow builds a release from a tag and creates a
draft GitHub release:

1. Secrets of a `release` environment hold the Developer ID Application
   certificate (base64 `.p12` and its password) and an App Store Connect API key
   (`.p8`, key ID, issuer ID). The API key is used rather than an Apple ID
   and app-specific password: it is scoped, revocable and not a person's
   login.
2. A temporary keychain: decode the `.p12`, create and unlock the keychain,
   `security import -T /usr/bin/codesign`, `set-key-partition-list`, and
   check `find-identity` finds "Developer ID Application". The keychain is
   deleted at the end of the job.
3. `codesign --options runtime --timestamp`, inside out: the bundled uv
   first, then the app.
4. `xcrun notarytool submit --wait` with the API key, then
   `xcrun stapler staple`, then `spctl -a -vv` as the check that Gatekeeper
   accepts it.
5. The zip (and a DMG) with the voiced wheel built from the same commit go
   on the draft release; a person publishes it. The cask lives in the
   project's tap first and goes to `homebrew/cask` once releases are
   notarized; at about 16 MB zipped the app is in the usual range there.

`menubar/Scripts/SIGNING.md` gains these steps.

As built (`.github/workflows/release.yml`), pushing a `v*` tag starts it
rather than a manual start: the tag is already the deliberate step, and
`workflow_dispatch` stays as a dry run that builds the same files and
creates no release (ad hoc on a branch, signed on a tag ref). The secrets sit in a `release` environment that
only `v*` tags may use, with a required reviewer, and a tag ruleset limits
who may create `v*` tags: the environment alone limits which ref deploys,
not who pushes it. A run from a branch cannot read the secrets. Without
them a tag build still completes, ad hoc, with `-adhoc` in the asset names and a
warning in the job summary and the notes. The signing itself is
`menubar/Scripts/sign.sh`, which SIGNING.md also runs by hand. The release's
wheel is the one inside the app, and the formula's `url` and `sha256` and
the cask (`packaging/homebrew/cask/voiced.rb`) come out as a workflow
artifact; the owner copies them to the tap.

## Where the logic lives, and the change to ADR 0014

Swift orchestrates and Python keeps the knowledge. The app runs three kinds
of child process: the bundled `uv` (environment, update, repair), the
environment's `voiced` CLI as the user (`install --json-progress`,
`uninstall`, `restart all`), and the privileged script through one
authorization prompt. Paths, the model list and every uninstall safety
rule from ADR 0006 stay in Python. The one exception is the plist
contents: the privileged script renders them itself, from the user name
alone, so root never parses a file the user wrote. They are fixed values,
and the prototype's test checks they equal what `voiced install` stages, so
the CLI and the app cannot install different daemons.

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
| Application firewall exception | Only if the firewall is on | Same prompt as above, no second one. The services' Python is signed ad hoc, and nobody can answer the firewall's "accept incoming connections" prompt before login, so Home Assistant on the LAN would be silently blocked. If `socketfilterfw --getglobalstate` reports it on, the privileged step adds and unblocks the active environment's Python: resolved as the user through `~/.voiced/bin/voiced`, re-resolved by root right before it is added, and accepted only as a regular file (not a symlink) owned by the user inside the home (compared with the home's real path, so a home reached through a symlink works), as it is for app and `--source` environments. In effect the exception says "this user's Python may accept connections"; signed programs already pass by default. It runs again on an update whenever the resolved path changes (a new Python version). A failure here is a warning, not a failed install. A Homebrew environment's Python is shared with every brewed Python program, so the app does not allow it and says so; that user can allow it in System Settings > Network > Firewall. | One line on the approval screen: "Your firewall is on; voiced will allow its services to accept connections." |
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

- Already in place (ADR 0017): the shared layout, `install.json`,
  `--origin app`, the install lock, `install_steps`, the one install mode,
  the watchdog's self-restart and its Homebrew-lock wait, and the services
  opening their own logs (ADR 0006).
- `voiced install --json-progress` with the versioned protocol above,
  emitted from `install_steps`.
- No other new flags: no `--app-bundle-id` (the privileged script adds
  `AssociatedBundleIdentifiers`), no `--skip-venv` (the one install mode
  already uses the running environment), no `--uv`, no `--remove-uv-state` (the uv state
  lives in the state directory and goes with it), no `--version --json`
  (`hello` and `install.json` carry the version). `voiced uninstall
  --json` is optional.
- The watchdog's `hello` adds `install.json`'s version, lock hash and
  Python.
- `UV_NO_CONFIG=1` for the CLI's `--source` build (ADR 0017 already puts
  its Python under the state directory with `--managed-python`).
- The services take `HF_HOME`, the espeak-ng overrides and a custom
  `log_dir` from the config file instead of the plist environment, so the
  plists carry only the fixed environment the privileged script renders.
- The ffmpeg and model items above, each with tests: `imageio-ffmpeg` in
  core dependencies and preferred; `download_patterns()`, pinned revisions
  and `snapshot_download` progress; a port check before approval. (The
  espeak-ng clone and length check are done, ADR 0009.) All done. The
  `ports` step binds each of the four configured ports as the services
  do (`bind()` sees every account's sockets) and asks `lsof` who listens
  (with `SO_REUSEADDR` a listener on 127.0.0.1 does not stop a bind on
  0.0.0.0, yet would answer local clients in voiced's place). It refuses
  with each port, its config key and its holder ("port 10300 ([stt] wyoming_port) is in use by python3.12 (pid
  812)"). A holder `lsof`, run as the user, cannot see is "another account
  or the system". A listener that runs the entry point (`<interpreter>
  ~/.voiced/bin/voiced serve ...`) is voiced's own and not a conflict;
  without `lsof` a port in use is said, not refused. Minutes pass between
  the check and the services starting, so it catches the common case, not
  a race.
- The CLI prints a `sudo` line for the same privileged script; it has no
  root script of its own (see "One script for the app and the CLI").

### App side

- An `Installer` module in `VoicedMenuBarUI`: a state machine over the
  steps above, testable with a fake runner the way `FakeControlServer` is
  used today; the welcome, progress, Connect Home Assistant and done views.
- `Contents/Helpers/uv`, the voiced wheel and a copy of its
  `requirements.lock`, `install-manifest.json`, the uv licence and the
  third-party notice in `Contents/Resources`; the privileged script
  compiled into the executable. `bundle.sh` fetches uv at a pinned version
  and checksum and takes the lock from the wheel it bundles.
- The bundle self-check before the prompt, the translocation check, the
  launch re-check with its Login Items card, the icon states, the hidden
  Debug pane, "What's new", and the Update, Roll back, Repair and
  Uninstall actions.
- The release workflow above.
- `InstallProbe` stays as it is: the plists come from the privileged
  script whichever path ran it, and the state directory, where the socket
  lives, is always `~/.voiced`.

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
  reviewed like security code: the user name as its only input, plists
  rendered from fixed values, no log paths, no inherited environment.
- Each release builds the app, the voiced wheel and the lock from one
  commit in one job.
- One install per Mac. A second account sees who has it.

## Open items for the owner

Two decisions:

- **Developer ID ($99 a year).** It blocks the cask and any download a
  non-technical user can open: Gatekeeper on macOS 15 blocks an
  unnotarized app. It does not block the implementation or development
  builds, which work ad hoc today. `menubar/Scripts/SIGNING.md` has the
  steps once the certificate exists.
- **Tag v0.1.0 and claim the `voiced` name on PyPI.** The tag gives the
  cask its release asset and lets the formula drop `--HEAD`. The PyPI name
  is needed only for `pip install voiced` and `uv tool install voiced`;
  neither the app (it ships the wheel) nor Homebrew needs it.

Needs the owner at the keyboard during implementation:

- One admin-prompt probe: `BASH_ENV`, and `SHELLOPTS=xtrace` with a
  command substitution in `PS4`, set in the app's environment (for example
  with `launchctl setenv` before launching it) must not reach the root
  shell (see the privileged step).
- "Background Items Added" with `AssociatedBundleIdentifiers` set, and the
  firewall exception with a LAN client, on a real install.
- Agree on the development override: a user default (`installSource`)
  pointing at a local wheel or checkout, so an unpushed development build
  of the app can install the matching voiced; release builds ignore it.
  Built as a debug-only override (`#if DEBUG`; `bundle.sh --debug` makes a
  bundle with it): it takes a wheel, and installs the lock that wheel
  carries. Release builds compile it out, so a default written by another
  process cannot change what a shipped app installs.
- Login Items: confirm that a voiced daemon switched off under Allow in
  the Background reads as `requiresApproval` from
  `SMAppService.statusForLegacyPlist(at:)`, which the app takes as off.
  The Debug pane shows the raw status of each service.

## Amendment (ADR 0022)

`voiced.models.PINS` and the engines' `download_patterns()` are replaced by
the model catalogue (`voiced/models/catalogue.py`: repository, licence,
gated, patterns) and its generated lock (`voiced/models/models.lock.json`:
revision, files with size and sha256, earlier pins). `python -I -m
voiced.models` still prints the default models for `install-manifest.json`,
from the lock. The models step still runs `python -m voiced.models download
--config` in the new environment; it now checks the disk first, and the
progress protocol's `error` event carries a `code` (`failed` for the
installer's own errors). The `download` event's `model` is the model's id in the
catalogue, which is its repository for every model so far.

## Amendment (2026-10-23)

Until now a first install downloaded both default models, and opening the
app showed only the menu, where Install voiced opened the installer. Some
people want only one service (another app does the
listening), or a smaller model, and should not wait for 2.9 GB they will not
use.

**The welcome screen is the setup.** On one screen: Speech to text and Text
to speech, each on or off, each with a model picker over the catalogue (ADR
0022) with the recommended model preselected; under the picker the chosen
model as a row of the Models pane shows it (`ModelRow` and the same summary
view: licence, Recommended, Quality and Speed, what it is for, size). Gated
models are left out, since they need a Hugging Face token first; Settings
has them. Under a switch that is off: nothing is downloaded for it, its
service loads no model and opens no port, and Settings turns it on later.
Below that, what goes where, the download size of this choice (the locked
wheels plus each chosen model and its engine's shared models), free disk
space, and one line saying an administrator password is asked once at the
end. Both services off disables Install. The approval step after `voiced
install` is unchanged; the move of the services to LaunchAgents, which
removes the prompt, is a separate change.

**Opening the app.** The app opens the installer by itself when it starts
with nothing installed (no plists in `/Library/LaunchDaemons`, not
translocated), so the first open is the setup; an install the CLI, Homebrew
or an older app made is left to the menu, as before. Update, Roll back,
Repair and Uninstall keep their flows and never show the setup.

**Back to the setup.** A first install that fails at `voiced install`
(perhaps on the model chosen: Moonshine below macOS 15, a download that
keeps failing) offers "Change Choices" beside Try Again, and closing the
window there goes back to the setup too; Install then runs again in the
environment already built. Closing the setup unused, or uninstalling,
keeps it from opening by itself at the next launch (a user default); the
menu still offers it.

**What is written when.** The app runs `voiced install --origin app
--json-progress --set KEY=VALUE ...` with `stt.enabled`, and for a service
that is on its `engine` and `model`, the same for `tts`; `--set` takes these
six keys only, parsed as `voiced settings set` parses them. Nothing the
daemons use changes before the models are in (ADR 0017), and a configured
model is a downloaded one (ADR 0022), so the config is not written first:
`voiced install` applies the changes in memory (`staged_settings`, the
coercion and validation `apply_settings` uses, the engines checked against
the voiced that runs the install, the new environment's for the app) and
plans from those settings (a service that is off has its ports left out);
the models step gets the same `--set` arguments and downloads for them,
nothing for a service that is off (this changes ADR 0033's last
consequence). The config itself is written at the switch, as the `settings`
step after `models` and before `link`, through `apply_settings` with every
check `voiced settings set` makes, the engines listed by the environment
being installed. A download that fails leaves the config as it was. The
first install's choice replaces what a config kept from an earlier
uninstall said: the screen shows what will run. Update, Repair and Roll
back pass no `--set`.

**Where the setup's list comes from.** No voiced is installed when it is
shown, so `voiced models list --json` cannot run. `python -I -m
voiced.models` (standard library only) now prints `{"catalogue":
{"engines": [...]}}`: every catalogued engine and model with the fields of
the list that do not depend on the cache or the installed engines
(`voiced.models.lockfile.catalogue_view`, which the list starts from, so
the two cannot drift), the recommended ones marked. `scripts/app_manifest.py`
copies it into `install-manifest.json` in place of the default models,
after checking each model is pinned to a commit with a size; a catalogue
test checks there is one recommended model per service and that neither
needs a token. The app decodes it with the Models pane's own `ModelList`
and derives from it what the default models were (the recommended choice).
Engine-level languages and whether an engine can run on this Mac need the
engines themselves, so the setup shows a model's languages as the pane
does, in its note.

A failed download's manual fallback names the models of that run at their
pinned revisions: the chosen ones on a first install, the configured ones
(as the services report them) on an update or repair. The uninstall box
for the models no longer gives a size, which only the cache knows. With a
service off, the Connect Home Assistant screen names only the Wyoming port
that is open.
