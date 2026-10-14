# voiced menu bar app

A small native macOS menu bar app for voiced. One glance at the icon shows
whether both services are up; the window behind it shows each service's
state, engine and model, warmth, uptime and restarts, restarts either service
in one click, opens the status page, the logs folder and the config file, and
changes the common settings. When voiced is not installed it shows the
install commands instead.

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
`io.github.julianbeaulieu.voiced.menubar` and the version from the repo's
`pyproject.toml`. The bundle is ad-hoc signed, which has no identity and is
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
| `not-installed` | Status reports `installed: false` |
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

`swift run MenuBarSnapshots <directory>` renders the window in each state, in
light and dark, to PNG files through the same fake server.

## How it finds the socket

1. The `VOICED_CONTROL_SOCKET` environment variable.
2. The `controlSocketPath` user default:
   `defaults write io.github.julianbeaulieu.voiced.menubar controlSocketPath /path/to/control.sock`.
3. `~/.voiced/control.sock`, the default state directory of the install
   layout.

When nothing answers there, the app checks for a voiced watchdog plist in
`/Library/LaunchDaemons` to tell "not installed" from "installed but not
running", and retries the socket with a backoff from 0.5 s up to 15 s.

## Layout

| Path | Contents |
|---|---|
| `Sources/VoicedControl` | Line framing, the control client (`NWConnection` over the Unix socket), payload models, the reduction from payloads to what the window shows, settings edits. No UI. |
| `Sources/VoicedMenuBarUI` | The observable app model (connection, reconnect, actions) and the SwiftUI views. |
| `Sources/VoicedMenuBar` | The `@main` app with its `MenuBarExtra`. |
| `Sources/FakeControlServerKit`, `Sources/FakeControlServer` | The fake control server and its command line. |
| `Sources/MenuBarSnapshots` | Renders the window in each state to PNG. |
| `Tests/VoicedControlTests` | Framing, client dispatch, reconnects and settings edits against the fake server; payload reduction. |

The protocol itself is described in the control channel documentation of the
main project; the app speaks protocol 1 and refuses any other version with a
message saying which side to update. The reasons for the design are in
[ADR 0013](../docs/decisions/0013-menu-bar-app.md).
