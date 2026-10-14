# 0013: Menu bar app

Date: 2026-10-15. Status: accepted.

## Context

voiced runs as three LaunchDaemons with no user interface. Knowing whether
it is up, restarting a service, finding the logs or switching the bind
address all mean a terminal, `voiced status` and editing TOML. Most people
who run a voice assistant at home want to glance at an icon and click. The
watchdog already supervises both services as the installing user and now
serves a local control socket (`<state dir>/control.sock`, newline JSON,
protocol 1) with status, settings, engine lists, restarts and events, so a
client needs nothing else.

## Decision

A native menu bar app in `menubar/`, a Swift package (Swift 6, SwiftUI,
`MenuBarExtra` in its window style), no third-party packages.

- **Native and SwiftUI.** A menu bar extra, `SMAppService` for launch at
  login, system fonts, SF Symbols, light and dark mode and the system's
  text sizes come with the platform. The app stays small: a release binary
  of about 1 MB, no runtime to ship. A web view or an Electron shell would
  be larger and look foreign; a Python app (rumps, PyObjC) would put a GUI
  dependency into the service environment and still not look native.
- **It speaks only the control socket.** It never reads or writes the config
  file, never runs `launchctl` and never calls the HTTP API. The watchdog is
  the one place that validates settings (through `Settings`), writes the
  config atomically and restarts services without root, so the app cannot
  disagree with the CLI or bypass a rule such as the LAN-without-key
  warning. It opens no network connection; a Unix socket with a peer uid
  check is reachable only by the same user on the same Mac. The only other
  thing it looks at is whether a watchdog plist exists in
  `/Library/LaunchDaemons`, to tell "not installed" from "not running" when
  the socket is absent.
- **In this repository.** The app and the server side of the protocol change
  together; one pull request can change both and one CI run checks both.
  The version comes from `pyproject.toml`, so app and services ship as one
  version. The package lives in its own directory with its own CI job, so
  the Python tooling never sees it.
- **macOS 14 floor**, the same as voiced itself. `MenuBarExtra` needs 13;
  14 adds the Observation framework (`@Observable`), which the app model
  uses, and nothing older needs supporting.
- **A fake control server** (`voiced-fake-control`, Swift, in the same
  package) serves the protocol with scripted states. Tests run the real
  client against it in process, and the app can be driven by hand without
  voiced. Swift rather than a Python script: the tests need it in process,
  and it reuses the app's payload models, so the two cannot drift apart.

Protocol handling: the client refuses any `hello` whose protocol is not 1,
with a message saying which side to update; unknown fields and events are
ignored; requests time out after 10 s; a lost connection is retried with a
backoff from 0.5 s to 15 s.

## Alternatives considered

- **A separate repository.** Cleaner for a Swift contributor, but every
  protocol change would need two coordinated releases.
- **AppKit `NSStatusItem` with `NSMenu`.** Fewer surprises in a plain menu,
  but pickers, toggles and a secure field are awkward in a menu; a
  SwiftUI window keeps the settings in one place without opening a separate
  window.
- **Talking to the HTTP API and `launchctl` directly.** Needs the API key,
  duplicates the watchdog's restart logic and cannot write settings safely.

## Consequences

- CI builds and tests the package on a macOS runner with Xcode 16 or newer.
- The bundle from `menubar/Scripts/bundle.sh` is ad-hoc signed only;
  distributing it needs a Developer ID signature and notarization
  (`menubar/Scripts/SIGNING.md`).
- Without the watchdog there is no socket, so the app shows "not running"
  even when both services are up; the watchdog is part of every install.
