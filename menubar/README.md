# voiced menu bar app

A small native macOS menu bar app for voiced. One glance at the icon shows
whether both services are up; the window behind it shows each service's
state, engine and model, uptime and restarts (and the last error when one is
degraded), restarts either service in one click, and opens the status page,
the logs folder and the config file. "Settings…" (⌘,) opens a standard
Settings window with a sidebar of panes: General (open at login, settings
from the status page), Network (listen on this Mac or the local network,
Home Assistant discovery, the API key), Speech to Text and Text to Speech
(a row per model, grouped by engine, to download, delete or use it; see
[Models](#models); Text to Speech also has the default voice, with a play
button that speaks a short line in it, and the default speed; see
[Voice and speed](#voice-and-speed)), About (versions, the
control socket, the config file and logs, the third-party notices) and, once
"Show Debug pane" in General turns it on, Debug (the app, its pins, the installed environment,
the installer's state, each service's Login Items status and every path,
with "Copy Diagnostics"). The window ends with the app's
version. When voiced is not installed the window offers the
installer (see below), or points to the installation guide when the app was
built without what the installer needs; when it cannot reach voiced it says
why.

It talks only to voiced's local control socket, a Unix socket served by the
watchdog; even a voice preview comes through it. It opens no network
connection outside the installer and its own updates: no analytics. Opening
the status page hands the URL voiced reports to your browser.

It needs macOS 14 or newer on Apple Silicon. Install it with
`brew install --cask julianbeaulieu/voiced/voiced`, or download the DMG
from the [release page](https://github.com/JulianBeaulieu/voiced/releases/latest);
the released app is signed and notarized, so macOS only asks once whether
to open an app downloaded from the internet. The rest of this page is for
working on the app. Building it needs Swift 6 (Xcode 16 or newer, or the
matching command line tools); SwiftPM fetches the one third-party package,
Sparkle, at the version `Package.resolved` pins.

## The icon

Four states (ADR 0018 and 0034), each a template image, so the menu bar
draws it for light and dark:

| State | Image | Meaning |
|---|---|---|
| idle | `waveform` | Every service that is on running, its model loaded once |
| update | `waveform` with a small plain dot at the top, drawn by `MenuBarIconImage` | Running, and a newer app is waiting to be installed (ADR 0034) |
| busy | `waveform` with a dotted badge, drawn by `MenuBarIconImage` | Starting, loading a model, connecting, or installing |
| error | `waveform.badge.exclamationmark` | Degraded, a service stopped, voiced not running, not installed, or an incompatible version |

## Build and test

```sh
cd menubar
swift build -c release
swift test
```

`Scripts/bundle.sh` builds `build/voiced.app`: the release binary in an app
bundle with `LSUIElement` (no Dock icon), bundle id
`io.github.julianbeaulieu.voiced.menubar`, the version from the repo's
`pyproject.toml` (mapped to X.Y.Z) and an icon drawn by
`Scripts/make-icon.swift` from the `waveform` symbol, with what the
installer needs (ADR 0018, "What the app ships and pins"):

| In the bundle | What it is |
|---|---|
| `Contents/Helpers/uv` | uv at the version and sha256 pinned in `bundle.sh` |
| `Contents/Frameworks/Sparkle.framework` | Sparkle, the app's updater, as SwiftPM built it at the version `Package.resolved` pins; the executable finds it through an `@executable_path/../Frameworks` rpath |
| `Contents/Resources/voiced-*.whl` | The voiced wheel, built from this checkout with that uv |
| `Contents/Resources/requirements.lock` | The lock the wheel carries |
| `Contents/Resources/install-manifest.json` | The voiced version, the CPython patch version (`app-python-version`), the wheel's and the lock's sha256, the download size of the locked wheels (from `uv.lock`), and the model catalogue the setup offers, each model with its revision and size, the defaults marked recommended; written by `scripts/app_manifest.py` |
| `Contents/Resources/uv-LICENSE` | uv's licence text, fetched at the same tag and checked |
| `Contents/Resources/Sparkle-LICENSE` | Sparkle's licence text, from the archive SwiftPM fetched |
| `Contents/Resources/NOTICE` | The repository's third-party notice (`scripts/notice.py` writes it from the lock) |
| `Contents/Resources/WhatsNew.md` | This version's section of `CHANGELOG.md`, else the Unreleased one |

The catalogue is what the wheel's `python -I -m voiced.models` prints:
every catalogued engine and model as `voiced models list --json` shows
them, without the cache, pinned by `voiced/models.lock.json`. Everything fetched is pinned by
hash: uv and its licence in `bundle.sh`, and the build backend and
`packaging` (which `app_manifest.py` uses to read the lock's markers and wheel
tags) by `src/voiced/build-requirements.lock`. The first run needs the
network; downloads are cached in `build/downloads` and checked against their
sha256 each time. Building uses the pinned uv, so no uv needs to be
installed. The bundle is about 30 MB, most of it uv.

`bundle.sh` refuses a checkout with uncommitted changes, since the bundle
would not match any commit; `--dirty` builds one anyway. `--debug` builds the
app in debug configuration, for the development override below.

A release build's Info.plist also names the update feed and the key updates
are signed with (below, "Updates of the app"); a `--debug` build has neither.

The bundle is ad-hoc signed inside out by `Scripts/codesign-app.sh` (uv,
Sparkle's helpers and the framework, then the app), which has no
identity and is not notarized. Releases are signed and notarized by
`Scripts/sign.sh` in the release workflow;
[Scripts/SIGNING.md](Scripts/SIGNING.md) has the details.

```sh
menubar/Scripts/bundle.sh
open menubar/build/voiced.app
```

Launch at login (`SMAppService`) is offered only when the app runs from a
bundle; `swift run` starts a bare executable that has nothing to register.

## Voice and speed

The Text to Speech pane's first section sets the defaults a request gets
when it names no voice or speed.

- **Voice** lists the voices of the engine in use. The play button beside
  it speaks one line in that voice, naming it in its own language ("Hi, I'm
  Heart.", "Bonjour, je m'appelle ..."; English for a language the app has
  no line for, and "Hi, this is my voice." for a voice without a name), at
  the default speed. The name is the voice's personal name from
  `engines.list` (`voice_names`); a voice whose name only describes it,
  such as OmniVoice's "Middle-aged", gets the line without a name. While
  the line loads (a model unloaded while idle loads first) the button
  shows a spinner, and while it plays, stop. The app asks the watchdog
  (`tts.speak`) on a connection of its own, which stop closes, so the
  service stops synthesizing and the app's other requests never wait; the
  watchdog asks the running service with the API key it started it with,
  so the app never needs the key. The button is off, with the reason under
  the picker, while text to speech is off, stopped, starting or missing its
  model; a failure is one line there.
- **Speed** steps from 0.5x to 2x by 0.1. The service reads it for each
  request, so it applies at once with no restart. A value set by hand
  outside that range is shown as it is, and the first step brings it in.

## Models

The Speech to Text and Text to Speech panes show every model voiced can run
(ADR 0022): its licence, linked to the model card, its size on disk or to
download, and its state, with the actions that state allows.

| State | Actions |
|---|---|
| Not downloaded | Download, Use |
| Downloading | a byte bar with speed and time left, Cancel |
| Downloaded | Use, Delete |
| In use | Delete disabled: choose another model first |
| Update available (an older pinned version cached) | Update, Use |
| Needs a Hugging Face token (gated) | Download, after `hf auth login` in Terminal |

A row with an older pinned version cached also offers "Delete Old Version".
Use downloads the model first if needed, sends engine and model together
through `settings.set` and restarts the service; after a text to speech
switch it says where to pick the new voice in Home Assistant. The footer
gives what voiced's models take, the cache folder with Show in Finder, and
that the folder is shared with other apps.

The app runs `~/.voiced/bin/voiced models list --json`, `download <model>
--json-progress` and `delete <model> --yes --json` as its own children
(docs/operations.md has their shapes), so the rows work while the services
are down, and a download goes on with the Settings window closed: the icon
shows busy, the menu window has a progress row with Cancel, quitting asks
first, and Update and Repair wait for it. A failed download's error code
becomes what to do on its row. A service whose model is missing
(`missing_model`) says so in the menu window, with Download, which fetches
the model and restarts the service.

## Run it against the fake server

`voiced-fake-control` serves the control protocol with scripted state, so the
app can be run and looked at without voiced:

```sh
cd menubar
swift run voiced-fake-control --scenario degraded --socket /tmp/voiced-fake.sock
# in a second terminal
VOICED_CONTROL_SOCKET=/tmp/voiced-fake.sock \
  VOICED_EXECUTABLE=$PWD/.build/debug/voiced-fake-control swift run voiced-menubar
```

`VOICED_EXECUTABLE` (debug builds only) makes the model rows run the fake's
`voiced-fake-control models ...` in place of `~/.voiced/bin/voiced`. The
fake's cache is a file next to the socket (`<socket>.models.json`), written
with the scenario's models at start: one model in each state, Phonon's gated
40m failing with `token_missing`, Nemotron's English model serving only
`en-US`, and Moonshine listed as unable to run on this Mac, as on macOS 14.
The fake command changes it (a download takes five seconds; SIGTERM ends it
with exit 143), the fake server reads it: `settings.set` refuses a model that
is not downloaded with `model_not_downloaded`, and a restarted service without
its model reports `missing_model`; the fake `delete` refuses the configured
models with `in_use`. To try other failures, edit the file's `failures` (model
id to an error code such as `network` or `disk`); while
`<socket>.models.json.busy` exists, download and delete fail with `busy`.
Pointed at a development checkout's `voiced`, the rows run the real command;
`VOICED_REAL_CLI=<voiced> swift test --filter RealCLITests` runs a list, a
cancelled download and a download through the app's code.

| Scenario | State |
|---|---|
| `running` | Both services running and warm |
| `starting` | Text to speech loading its model |
| `degraded` | Text to speech degraded, restarted once |
| `stopped` | Both services stopped |
| `not-installed` | Watchdog run by hand: `installed: false`, services running |
| `lan-without-key` | Bound to the LAN with no API key |
| `newer-protocol` | Says hello with protocol 2, which the app refuses |
| `cycle` | Moves through running, starting, degraded and stopped every few seconds |
| `missing-model` | Text to speech up, its model (Kokoro) not downloaded |

The fake server answers every operation of the contract: settings changes
are validated (an engine with another engine's model or voice is refused with
`invalid_setting`), report `restart_required` and the `lan_without_api_key`
warning, and are pushed to other clients as `settings` events; restarts go
through `starting` to `running` as `status` events; `status` is pushed every
10 seconds (`--status-interval`). Stop the server and the app shows voiced as
not running, start it again and the app reconnects.

Pointing the app at a socket that does not exist shows the "not installed"
or "not running" state, depending on whether a voiced watchdog plist is in
`~/Library/LaunchAgents`.

`swift run MenuBarSnapshots <directory>` renders the menu bar window, the
Settings window, each installer screen, the launch cards, the icons and
"What's new", in light and dark, to PNG files
through the same fake server and the fake install runner.

## Snapshots in a pull request

A pull request that changes the UI shows the pictures it changes in its
description, so a review sees the screens without building the app. They
do not go on the pull request's branch: render them, put the changed ones,
light and dark, alone in one commit on a branch of their own,
`snapshots/pr-<number>`, with no history, push it, and embed each picture
by that commit's SHA, light and dark side by side. A worktree keeps your
checkout as it is (`git worktree add --orphan` needs git 2.42 or later):

```sh
(cd menubar && swift run MenuBarSnapshots /tmp/shots)
git worktree add --orphan -b snapshots/pr-123 /tmp/pr-123-shots
cp /tmp/shots/settings-tts-light.png /tmp/shots/settings-tts-dark.png /tmp/pr-123-shots/
git -C /tmp/pr-123-shots add . && git -C /tmp/pr-123-shots commit -m "docs: snapshots for pull request 123"
git -C /tmp/pr-123-shots push origin snapshots/pr-123
git worktree remove /tmp/pr-123-shots
```

```markdown
![light](https://raw.githubusercontent.com/JulianBeaulieu/voiced/<sha>/settings-tts-light.png)
```

From a fork, push the branch to your fork and put your account in place of
`JulianBeaulieu` in the URL. Linked by SHA, a picture stays the one that
was reviewed after the branch moves. The branch is never merged, and it is kept after the merge so the
pictures keep loading.

## The installer

ADR 0018 has the design. The installer is a window with one screen per step,
and the app opens it by itself when it starts with nothing installed (unless
this version of the app saw the setup closed unused, or voiced uninstalled):
the welcome, which is the setup (each service on or off with its model, the
recommended ones chosen; what goes where, the download size of that choice,
free disk space), building
the environment with the bundled uv, `voiced install --origin app
--json-progress` with the setup's choice as `--set` settings, its steps and
download progress (its last step loads the LaunchAgents), waiting for the
watchdog's `hello`, Connect Home Assistant, and Done. Nothing asks for an
administrator password ([ADR 0035](../docs/decisions/0035-launch-agents.md)). Each step that fails shows its cause and
the last lines of output, with Try Again.

`Installer` (in `Sources/VoicedMenuBarUI/Installer`) sequences the steps,
which `InstallStages` implements, each returning what it made or why it
failed. They reach the Mac only through an `InstallRunner`:
`SystemInstallRunner` starts each command in a process group of its own and
shows the one prompt left (below), and the tests and the snapshots use `FakeInstallRunner`
(in `Tests/TestSupport`) with recorded lines of the progress protocol. The
services are reached through the app's own `AppModel`, against the fake
control server in tests. Nothing in `swift test` prompts, loads a launchd
job or touches `/Library`.

The one administrator prompt left removes the LaunchDaemons of voiced
0.4.0 or earlier, once, before `voiced install` puts the agents in their
place: Install, Update, Repair and Roll back show "Remove the old
background services" when this user's daemons are still in
`/Library/LaunchDaemons`, and Uninstall asks first. It runs fixed
`launchctl bootout` and `rm` commands (`LegacyDaemons.swift`, the same
commands `voiced install` prints for `sudo`) through `osascript` with an
environment of `PATH` alone, so nothing of the app's environment reaches
the shell that runs as root. Cancelled, the old services keep running.

The app offers the installer only when its bundle carries
`Contents/Helpers/uv` and, in `Contents/Resources`, `install-manifest.json`,
`requirements.lock` and the wheel the manifest names, as every bundle from
`bundle.sh` does; `swift run` has none of them and points to the guide.

### Update, Roll back, Repair and Uninstall

For an install the app made (`source = app` in `install.json`), the same
window runs ADR 0018's Lifecycle with the same stages in another order:

- **Update voiced** (the version card, or Settings > About): a new
  environment beside the current one, then its own `voiced install`, which
  reloads only the agents whose plists changed. The app waits for a `hello` from the new
  environment, runs `voiced restart all` if none comes within the startup
  grace, and afterwards removes the Pythons no environment uses (none when
  `~/.voiced`, `envs` or `python` is a link, or an environment's
  `pyvenv.cfg` cannot be read) and clears its uv cache.
- **Roll back**: the environment `install.json` names as `previous`, which
  pruning keeps, while it is still in `envs/`. Its own `voiced install`
  links it again. A failed update offers it once the new environment is
  linked; Settings > About offers it afterwards.
- **Repair** (Settings > About, or the menu while the services do not
  answer): the bundled lock and wheel reinstalled into the active
  environment, or, when its Python is gone, a fresh one built after the old
  one is moved to `envs/<id>.broken`; then `voiced install` and a restart.
- **Uninstall** (Settings > About): `voiced uninstall --yes`, which stops
  and removes the LaunchAgents, then removes only voiced's files under the
  install lock and refuses symbolic links. Models, logs and the config are
  boxes, unticked by default. Running it again after a failure is safe:
  when no `voiced` is left to run (an interrupted removal, a Python gone),
  the app unloads the agents and deletes their plists itself, removes the
  state directory by the same rules (`install.state_targets`), never
  through a link, and says the models, logs and config stayed. Dragging the
  app to the Trash leaves the services running; the app, anything left
  unticked, and a firewall entry for voiced's Python (if you allowed it)
  stay after Uninstall.

Homebrew's and the command line's installs are shown, never changed:
Settings > About lists the commands that update, repair and remove them,
with `voiced uninstall --yes` before `brew uninstall voiced`.

### Installing a development voiced

A debug build of the app can install another voiced than the one it carries,
such as an unpushed checkout, built into a wheel first:

```sh
(cd ~/src/voiced && uv build --wheel)
menubar/Scripts/bundle.sh --dirty --debug
defaults write io.github.julianbeaulieu.voiced.menubar installSource ~/src/voiced/dist/voiced-0.5.0-py3-none-any.whl
defaults delete io.github.julianbeaulieu.voiced.menubar installSource   # back to the bundled wheel
```

The lock installed is the one that wheel carries, and the models are the
bundle's pins. The override exists only in debug builds: a release build
(the default for `bundle.sh` and the release) compiles it out, so no process
can steer what a shipped app installs by writing a default. While it is in
effect the Debug pane shows it and the version card stays away.

## On every launch

Each launch, and each time the menu bar window opens, the app checks without
root (ADR 0018, "On every launch"):

- **Translocation.** Opened from Downloads without being moved, macOS runs
  the app from a random read-only copy. The window asks to move it to
  Applications ("Show in Finder") and offers no installer until then.
- **The installed voiced.** What `install.json` records (from the watchdog's
  `hello`, or the file when no watchdog answers) against the bundle's pins:
  the version and the lock's hash. A difference, either way, is shown; for a
  Homebrew install with `brew upgrade voiced`.
- **Login Items.** When the services are switched off in System Settings >
  General > Login Items, a card says so and opens Login Items. The app asks `SMAppService` off the main thread, at launch
  and every 2 s while the menu bar window is open; the Debug pane shows each
  service's raw status.

When the services first run this app's pins after running another version,
a "What's new" window shows the bundled notes, once. A first install shows
none.

## Updates of the app

The app updates itself with [Sparkle](https://sparkle-project.org)
(ADR 0034). It reads
`https://github.com/JulianBeaulieu/voiced/releases/latest/download/appcast.xml`
when it opens and once a day; **Check for updates automatically** in
Settings > General turns that off, and **Check for Updates…** in the menu
checks at once. A newer version is a card at the top of the menu bar
window: **Install and Relaunch** downloads it, checks its EdDSA signature
against the key in Info.plist, replaces the app and opens it again, and the
relaunched app then runs "Update voiced" for an install it manages, showing
the installer's progress. **Later** hides the card until the next check.
While voiced installs or a model downloads, Install and Relaunch waits and
the card says why, since quitting would stop them; if a quit is cancelled
anyway, **Relaunch Now** asks again. A found update adds a small dot to the
icon, never the error badge.

Sparkle shows none of its own windows: `Sources/VoicedMenuBar/SparkleUpdater.swift`,
the one file that imports it, hands what Sparkle reports to `AppUpdates`
(`Sources/VoicedMenuBarUI/Updates`), which the tests and the snapshots
drive with `FakeAppUpdater`. A build without a feed (`swift run`, a
`--debug` bundle) has no updater, and the menu and Settings leave it out.

To try it without a release, give a copy of the bundle another bundle
identifier, `SUFeedURL` on `http://127.0.0.1` and a throwaway key's
`SUPublicEDKey`, re-sign it with `Scripts/codesign-app.sh APP --sign -
--timestamp=none`, and serve a feed from `scripts/release.py appcast` for a
zip of a higher `CFBundleVersion` signed with `sign_update --ed-key-file -`
(`.build/artifacts/sparkle/Sparkle/bin`). ADR 0034, "Measured", has what
that run showed.

## How it finds the socket

1. The `VOICED_CONTROL_SOCKET` environment variable.
2. The `controlSocketPath` user default:
   `defaults write io.github.julianbeaulieu.voiced.menubar controlSocketPath /path/to/control.sock`.
3. `~/.voiced/control.sock`, the default state directory of the install
   layout.

The app does not read the config file, so an install with another state
directory (`[install] venv_dir`) needs one of the first two. When nothing
answers, the window says why (no socket, a stale socket, another user's
socket, something that is not a socket, no hello) and names the path; for a
missing socket it reads the `*.watchdog.plist` files in
`~/Library/LaunchAgents` (and, for an install from 0.4.0 or earlier,
`/Library/LaunchDaemons`) to tell "not installed" from "installed but not
running", whatever the label prefix. It retries with a backoff from 0.5 s up
to 15 s, and drops a connection that sends no event for 25 s.

On a crowded menu bar macOS can hide the icon. Launching the app again
(Finder, Spotlight, `open`) opens the Settings window.

## Layout

| Path | Contents |
|---|---|
| `Sources/VoicedControl` | Line framing, the control client (`NWConnection` over the Unix socket), payload models and `SettingKey`, the reduction from payloads to what the windows show, settings edits, the checks on what `open` may open, socket discovery and the install probe. No UI. |
| `Sources/VoicedMenuBarUI` | The observable app model (connection, liveness, reconnect, actions) and the SwiftUI views. `Installer/` holds the installer: the state machine, the progress protocol, the runner, the removal of an earlier release's LaunchDaemons and the development override. `Launch/` holds the launch checks, What's new and the diagnostics. |
| `Sources/VoicedMenuBar` | The `@main` app with its `MenuBarExtra` and `Settings` scenes, and Sparkle behind `AppUpdater`. |
| `Sources/FakeControlServerKit`, `Sources/FakeControlServer` | The fake control server and its command line. |
| `Sources/MenuBarSnapshots` | Renders the menu bar window, the Settings window and each installer screen to PNG. |
| `Tests/VoicedControlTests` | Framing, client dispatch, connection failures and cancellation against the fake server; payload reduction; settings edits; open checks; install probe. |
| `Tests/VoicedMenuBarUITests` | The app model against the fake server: reconnects, liveness, start and stop, settings, restarts, warnings. The installer through every step and failure, Update, Roll back, Repair and Uninstall, the migration from LaunchDaemons, the progress protocol, the administrator prompt and the process runner. The launch checks, What's new, the diagnostics and the development override. |
| `Tests/TestSupport` | Helpers shared by both, and by the snapshots: the fake install runner, recorded progress lines and the fake updater. |

The protocol is the control channel's
([ADR 0013](../docs/decisions/0013-local-control-channel.md)); the app speaks
protocol 1 and refuses any other version with a message saying which side to
update. The reasons for the app's design are in
[ADR 0014](../docs/decisions/0014-menu-bar-app.md).
