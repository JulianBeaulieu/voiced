# voiced menu bar app

A small native macOS menu bar app for voiced. One glance at the icon shows
whether both services are up; the window behind it shows each service's
state, engine and model, uptime and restarts (and the last error when one is
degraded), restarts either service in one click, and opens the status page,
the logs folder and the config file. "Settings…" (⌘,) opens a standard
Settings window with a sidebar of panes: General (open at login, settings
from the status page), Network (listen on this Mac or the local network,
Home Assistant discovery, the API key), Speech to Text (engine), Text to
Speech (engine and voice) and About (versions, the control socket, the
config file and logs). When voiced is not installed the window offers the
installer (see below), or points to the installation guide when the app was
built without what the installer needs; when it cannot reach voiced it says
why.

It talks only to voiced's local control socket, a Unix socket served by the
watchdog. It opens no network connection: no analytics, no update checks.
Opening the status page hands the URL voiced reports to your browser.

Requirements: macOS 14 or newer, Swift 6 (Xcode 16 or newer, or the matching
command line tools). No third-party packages.

## The icon

| Icon | SF Symbol | Meaning |
|---|---|---|
| waveform | `waveform` | Both services running with their model loaded |
| waveform with a badge | `waveform.badge.exclamationmark` | Starting, loading a model, degraded, one service stopped, or still connecting |
| struck-through waveform | `waveform.slash` | Both services stopped, voiced not running, not installed, or an incompatible version |

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
`Scripts/make-icon.swift` from the `waveform` symbol. The bundle is ad-hoc signed, which has no identity and is
not notarized; [Scripts/SIGNING.md](Scripts/SIGNING.md) lists what signing
and notarizing for distribution takes.

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
Settings window and each installer screen, in light and dark, to PNG files
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

The script that runs as root is
`Sources/VoicedMenuBarUI/Installer/voiced-privileged.sh`. Its bytes are
compiled into the executable (`.embedInCode` in `Package.swift`); it is
never a file in the bundle. The app runs it through `osascript` with an
environment of `PATH` alone, so nothing of its own environment reaches the
shells that run as root. Run as a normal user, the script has a test mode
that writes to a scratch directory. `Scripts/test-privileged.sh` uses it to
compare what the script renders with the plists `voiced install` stages, and
checks every refusal. Stage the plists with the `voiced install` of the
same commit first, as the comparison is exact:

```sh
menubar/Scripts/test-privileged.sh ~/.config/voiced/launchd
```

The app offers the installer only when its bundle carries
`Contents/Helpers/uv` and, in `Contents/Resources`, `install-manifest.json`,
`requirements.lock` and the wheel the manifest names. `bundle.sh` does not
add them yet, so a bundle built today still points to the guide.

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
| `Sources/VoicedMenuBarUI` | The observable app model (connection, liveness, reconnect, actions) and the SwiftUI views. `Installer/` holds the installer: the state machine, the progress protocol, the runner and the privileged script. |
| `Sources/VoicedMenuBar` | The `@main` app with its `MenuBarExtra` and `Settings` scenes. |
| `Sources/FakeControlServerKit`, `Sources/FakeControlServer` | The fake control server and its command line. |
| `Sources/MenuBarSnapshots` | Renders the menu bar window, the Settings window and each installer screen to PNG. |
| `Tests/VoicedControlTests` | Framing, client dispatch, connection failures and cancellation against the fake server; payload reduction; settings edits; open checks; install probe. |
| `Tests/VoicedMenuBarUITests` | The app model against the fake server: reconnects, liveness, start and stop, settings, restarts, warnings. The installer through every step and failure, the progress protocol, the privileged script in its test mode and the process runner. |
| `Tests/TestSupport` | Helpers shared by both, and by the snapshots: the fake install runner and recorded progress lines. |

The protocol is the control channel's
([ADR 0013](../docs/decisions/0013-local-control-channel.md)); the app speaks
protocol 1 and refuses any other version with a message saying which side to
update. The reasons for the app's design are in
[ADR 0014](../docs/decisions/0014-menu-bar-app.md).
