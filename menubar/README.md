# voiced menu bar app

A small native macOS menu bar app for voiced. One glance at the icon shows
whether both services are up; the window behind it shows each service's
state, engine and model, uptime and restarts (and the last error when one is
degraded), restarts either service in one click, and opens the status page,
the logs folder and the config file. "Settings…" (⌘,) opens a standard
Settings window with a sidebar of panes: General (open at login, settings
from the status page), Network (listen on this Mac or the local network,
Home Assistant discovery, the API key), Speech to Text (engine), Text to
Speech (engine and voice), About (versions, the control socket, the
config file and logs, the third-party notices) and, once "Show Debug pane"
in General turns it on, Debug (the app, its pins, the installed environment,
the installer's state, each service's Login Items status and every path,
with "Copy Diagnostics"). The window ends with the app's
version. When voiced is not installed the window offers the
installer (see below), or points to the installation guide when the app was
built without what the installer needs; when it cannot reach voiced it says
why.

It talks only to voiced's local control socket, a Unix socket served by the
watchdog. It opens no network connection outside the installer: no
analytics, no update checks.
Opening the status page hands the URL voiced reports to your browser.

It needs macOS 14 or newer on Apple Silicon. Install it with
`brew install --cask julianbeaulieu/voiced/voiced`, or download the DMG
from the [release page](https://github.com/JulianBeaulieu/voiced/releases/latest);
the released app is signed and notarized, so macOS only asks once whether
to open an app downloaded from the internet. The rest of this page is for
working on the app. Building it needs Swift 6 (Xcode 16 or newer, or the
matching command line tools) and no third-party packages.

## The icon

Three states (ADR 0018), each a template image, so the menu bar draws it
for light and dark:

| State | Image | Meaning |
|---|---|---|
| idle | `waveform` | Both services running with their model loaded |
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
| `Contents/Resources/voiced-*.whl` | The voiced wheel, built from this checkout with that uv |
| `Contents/Resources/requirements.lock` | The lock the wheel carries |
| `Contents/Resources/install-manifest.json` | The voiced version, the CPython patch version (`app-python-version`), the wheel's and the lock's sha256, the download size of the locked wheels (from `uv.lock`), and each default model's repository, revision and size; written by `scripts/app_manifest.py` |
| `Contents/Resources/uv-LICENSE` | uv's licence text, fetched at the same tag and checked |
| `Contents/Resources/NOTICE` | The repository's third-party notice (`scripts/notice.py` writes it from the lock) |
| `Contents/Resources/WhatsNew.md` | This version's section of `CHANGELOG.md`, else the Unreleased one |

The model pins are what the wheel's `python -I -m voiced.models` prints
(`voiced.models.PINS`). Everything fetched is pinned by
hash: uv and its licence in `bundle.sh`, and the build backend and
`packaging` (which `app_manifest.py` uses to read the lock's markers and wheel
tags) by `src/voiced/build-requirements.lock`. The first run needs the
network; downloads are cached in `build/downloads` and checked against their
sha256 each time. Building uses the pinned uv, so no uv needs to be
installed. The bundle is about 30 MB, most of it uv.

`bundle.sh` refuses a checkout with uncommitted changes, since the bundle
would not match any commit; `--dirty` builds one anyway. `--debug` builds the
app in debug configuration, for the development override below.

The bundle is ad-hoc signed, uv first and then the app, which has no
identity and is not notarized. Releases are signed and notarized by
`Scripts/sign.sh` in the release workflow;
[Scripts/SIGNING.md](Scripts/SIGNING.md) has the details.

```sh
menubar/Scripts/bundle.sh
open menubar/build/voiced.app
```

Launch at login (`SMAppService`) is offered only when the app runs from a
bundle; `swift run` starts a bare executable that has nothing to register.

## Run it against the fake server

`voiced-fake-control` serves the control protocol with scripted state, so the
app can be run and looked at without voiced:

```sh
cd menubar
swift run voiced-fake-control --scenario degraded --socket /tmp/voiced-fake.sock
# in a second terminal
VOICED_CONTROL_SOCKET=/tmp/voiced-fake.sock swift run voiced-menubar
```

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

The fake server answers every operation of the contract: settings changes
are validated (an engine with another engine's model or voice is refused with
`invalid_setting`), report `restart_required` and the `lan_without_api_key`
warning, and are pushed to other clients as `settings` events; restarts go
through `starting` to `running` as `status` events; `status` is pushed every
10 seconds (`--status-interval`). Stop the server and the app shows voiced as
not running, start it again and the app reconnects.

Pointing the app at a socket that does not exist shows the "not installed"
or "not running" state, depending on whether a voiced watchdog plist is in
`/Library/LaunchDaemons`.

`swift run MenuBarSnapshots <directory>` renders the menu bar window, the
Settings window, each installer screen, the launch cards, the icons and
"What's new", in light and dark, to PNG files
through the same fake server and the fake install runner. Pull requests that change the UI attach these pictures.

## The installer

ADR 0018 has the design. The installer is a window with one screen per step:
the welcome (what goes where, the download size, free disk space), building
the environment with the bundled uv, `voiced install --origin app
--json-progress` with its steps and download progress, one administrator
prompt that runs the privileged script, waiting for the watchdog's `hello`,
Connect Home Assistant, and Done. Each step that fails shows its cause and
the last lines of output, with Try Again.

`Installer` (in `Sources/VoicedMenuBarUI/Installer`) sequences the steps,
which `InstallStages` implements, each returning what it made or why it
failed. They reach the Mac only through an `InstallRunner`:
`SystemInstallRunner` starts each command in a process group of its own and
shows the prompt, and the tests and the snapshots use `FakeInstallRunner`
(in `Tests/TestSupport`) with recorded lines of the progress protocol. The
services are reached through the app's own `AppModel`, against the fake
control server in tests. Nothing in `swift test` prompts, starts a daemon
or touches `/Library`.

The script that runs as root is `src/voiced/launchd/voiced-privileged.sh`
in the voiced package, the same file `voiced install` prints for `sudo`;
`Sources/VoicedMenuBarUI/Installer/voiced-privileged.sh` is a symlink to
it. Its bytes are compiled into the executable (`.embedInCode` in
`Package.swift`); it is never a file in the bundle, and a test compares the
compiled-in bytes with the packaged file. The app runs it through
`osascript` with an environment of `PATH` alone, so nothing of its own
environment reaches the shells that run as root. Run as a normal user, the
script has a test mode that writes to a scratch directory.
`Scripts/test-privileged.sh` uses it to check what the script renders and
every refusal; it needs nothing staged:

```sh
menubar/Scripts/test-privileged.sh
```

The app offers the installer only when its bundle carries
`Contents/Helpers/uv` and, in `Contents/Resources`, `install-manifest.json`,
`requirements.lock` and the wheel the manifest names, as every bundle from
`bundle.sh` does; `swift run` has none of them and points to the guide.

### Update, Roll back, Repair and Uninstall

For an install the app made (`source = app` in `install.json`), the same
window runs ADR 0018's Lifecycle with the same stages in another order:

- **Update voiced** (the version card, or Settings > About): a new
  environment beside the current one, its own `voiced install`, then
  `voiced-privileged check` as the user. The administrator prompt appears
  only if a plist differs, a label is not loaded, or the firewall is on and
  the services' Python changed. The app waits for a `hello` from the new
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
  one is moved to `envs/<id>.broken`; then `voiced install`, the check, and
  a restart.
- **Uninstall** (Settings > About): the privileged script's `uninstall`,
  then `voiced uninstall --yes`, which removes only voiced's files under the
  install lock and refuses symbolic links. Models, logs and the config are
  boxes, unticked by default. Running it again after a failure is safe:
  plists already removed need no prompt, and when no `voiced` is left to
  run (an interrupted removal, a Python gone), the app removes the state
  directory by the same rules (`install.state_targets`), never through a
  link, and says the models, logs and config stayed. Dragging the app to
  the Trash leaves the services running; the app, anything left unticked,
  and a firewall entry for voiced's Python (when the firewall is on) stay
  after Uninstall.

Homebrew's and the command line's installs are shown, never changed:
Settings > About lists the commands that update, repair and remove them,
with `voiced uninstall` and its root step before `brew uninstall voiced`.

### Installing a development voiced

A debug build of the app can install another voiced than the one it carries,
such as an unpushed checkout, built into a wheel first:

```sh
(cd ~/src/voiced && uv build --wheel)
menubar/Scripts/bundle.sh --dirty --debug
defaults write io.github.julianbeaulieu.voiced.menubar installSource ~/src/voiced/dist/voiced-0.2.0-py3-none-any.whl
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
  General > Login Items, a card says voiced can't start at boot and opens
  Login Items. The app asks `SMAppService` off the main thread, at launch
  and every 2 s while the menu bar window is open; the Debug pane shows each
  service's raw status.

When the services first run this app's pins after running another version,
a "What's new" window shows the bundled notes, once. A first install shows
none.

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
`/Library/LaunchDaemons` to tell "not installed" from "installed but not
running", whatever the label prefix. It retries with a backoff from 0.5 s up
to 15 s, and drops a connection that sends no event for 25 s.

On a crowded menu bar macOS can hide the icon. Launching the app again
(Finder, Spotlight, `open`) opens the Settings window.

## Layout

| Path | Contents |
|---|---|
| `Sources/VoicedControl` | Line framing, the control client (`NWConnection` over the Unix socket), payload models and `SettingKey`, the reduction from payloads to what the windows show, settings edits, the checks on what `open` may open, socket discovery and the install probe. No UI. |
| `Sources/VoicedMenuBarUI` | The observable app model (connection, liveness, reconnect, actions) and the SwiftUI views. `Installer/` holds the installer: the state machine, the progress protocol, the runner, the privileged script and the development override. `Launch/` holds the launch checks, What's new and the diagnostics. |
| `Sources/VoicedMenuBar` | The `@main` app with its `MenuBarExtra` and `Settings` scenes. |
| `Sources/FakeControlServerKit`, `Sources/FakeControlServer` | The fake control server and its command line. |
| `Sources/MenuBarSnapshots` | Renders the menu bar window, the Settings window and each installer screen to PNG. |
| `Tests/VoicedControlTests` | Framing, client dispatch, connection failures and cancellation against the fake server; payload reduction; settings edits; open checks; install probe. |
| `Tests/VoicedMenuBarUITests` | The app model against the fake server: reconnects, liveness, start and stop, settings, restarts, warnings. The installer through every step and failure, Update, Roll back, Repair and Uninstall, the progress protocol, the privileged script in its test mode and the process runner. The launch checks, What's new, the diagnostics and the development override. |
| `Tests/TestSupport` | Helpers shared by both, and by the snapshots: the fake install runner and recorded progress lines. |

The protocol is the control channel's
([ADR 0013](../docs/decisions/0013-local-control-channel.md)); the app speaks
protocol 1 and refuses any other version with a message saying which side to
update. The reasons for the app's design are in
[ADR 0014](../docs/decisions/0014-menu-bar-app.md).
