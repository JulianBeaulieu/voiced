# 0014: Menu bar app

Date: 2026-10-15. Status: accepted; amended by
[ADR 0018](0018-install-from-the-menu-bar-app.md).

## Context

voiced runs as three LaunchDaemons with no user interface. Knowing whether
it is up, restarting a service, finding the logs or switching the bind
address all mean a terminal, `voiced status` and editing TOML. Most people
who run a voice assistant at home want to glance at an icon and click. The
watchdog supervises both services as the installing user and serves the
local control socket ([ADR 0013](0013-local-control-channel.md):
`<state dir>/control.sock`, newline JSON, protocol 1) with status, settings,
engine lists, restarts and events, so a client needs nothing else.

## Decision

A native menu bar app in `menubar/`, a Swift package (Swift 6, SwiftUI,
`MenuBarExtra` in its window style, a standard `Settings` window), no
third-party packages.

- **Native and SwiftUI.** A menu bar extra, `SMAppService` for launch at
  login, system fonts, SF Symbols, light and dark mode and the system's
  text sizes come with the platform. The app stays small: a release binary
  of about 1 MB, no runtime to ship. A web view or an Electron shell would
  be larger and look foreign; a Python app (rumps, PyObjC) would put a GUI
  dependency into the service environment and still not look native.
- **It speaks only the control socket.** It never reads or writes the config
  file and never calls the HTTP API. It runs `launchctl` only inside the
  signed privileged script that installs and uninstalls the daemons, and
  reads whether a label is loaded (amended by ADR 0018). The watchdog is
  the one place that validates settings (through `Settings`), writes the
  config atomically and restarts services without root, so the app cannot
  disagree with the CLI or bypass a rule such as the LAN-without-key
  warning. It opens no network connection; a Unix socket with a peer uid
  check is reachable only by the same user on the same Mac. Besides the
  socket it reads only the LaunchDaemon plists, to tell "not installed"
  from "not running": voiced is installed when a `*.watchdog.plist` runs
  `... watchdog` as this user, whatever `[launchd] label_prefix` is.
  When voiced is not installed, the app opens its installer (ADR 0018)
  instead of linking to the install guide.
- **Finding the socket.** Because it does not read the config, the app
  cannot know a custom state directory (`[install] venv_dir`). It uses
  `VOICED_CONTROL_SOCKET`, then the `controlSocketPath` user default, then
  `~/.voiced/control.sock`. An install with another state directory shows
  "not running" until one of the first two points at the socket; the
  window names the path it tried.
  With the shared install layout (ADR 0017, used by ADR 0018) the state
  directory is always `~/.voiced`, so the default is right for every new
  install.
- **Status in the menu bar window, settings in a Settings window.** The
  menu bar window shows only status and one-click actions and stays one
  size; "Settings…" (⌘,) opens the standard Settings window, a fixed
  700 by 500 pt with a sidebar of panes (General, Network, Speech to Text,
  Text to Speech, About), so it fits a 13-inch screen and each pane is short
  enough to read without scrolling. The sidebar is a plain list next to the
  pane, not a split view, so it cannot be collapsed and draws the same in
  snapshots. An earlier version had both on tabs inside the menu bar
  window: it jumped from 340 to 609 pt high on the Settings tab and still
  scrolled the last sections out of sight; a later one put every setting
  in one form, which grew past a 13-inch screen. Notices that ask for action
  (restart needed, LAN without a key, warnings, errors) appear in both
  windows, so a change made in Settings is never silently pending.
  A menu bar app is not active when Settings opens, so the window would
  come up behind the frontmost app; the app activates itself and then
  brings forward the window whose identifier contains "Settings". That
  identifier is SwiftUI's own, not an API: if a SwiftUI release renames it,
  the window still opens, only behind other windows, and the lookup in
  `SettingsWindow` is the place to change.
- **In this repository.** The app and the server side of the protocol change
  together; one pull request can change both and one CI run checks both.
  The version comes from `pyproject.toml`, so app and services ship as one
  version. The package lives in its own directory with its own CI job, so
  the Python tooling never sees it.
- **macOS 14 floor**, the same as voiced itself. `MenuBarExtra` needs 13;
  14 adds the Observation framework (`@Observable`) and `openSettings`,
  which the app uses, and nothing older needs supporting.
- **A fake control server** (`voiced-fake-control`, Swift, in the same
  package) serves the protocol with scripted states. Tests run the real
  client against it in process, and the app can be driven by hand without
  voiced. Swift rather than a Python script: the tests need it in process,
  and it shares the app's payload models and `SettingKey`, so the two
  cannot drift apart.

Protocol handling: the client refuses any `hello` whose protocol is not 1,
with a message saying which side to update; unknown fields and events are
ignored; requests time out after 10 s. `service.restart` is answered at
once with the outcome per service in `detail`, and status events show the
restart itself; a service the server refused keeps its restart notice and
the refusal is shown. The app still allows 30 s for that answer and treats
a timeout as "requested", in case a server is slow to answer; a
connection with no event for 25 s is closed, since the server pushes
`status` at least every 10 s; a lost connection is retried with a backoff
from 0.5 s to 15 s, reset only once a connection has delivered `status`.
Every way a connection can fail (no socket, stale socket, another user's
socket, not a socket, no hello, closed before hello) has its own message,
and only the first two suggest `voiced start watchdog`.

## Alternatives considered

- **A separate repository.** Cleaner for a Swift contributor, but every
  protocol change would need two coordinated releases.
- **AppKit `NSStatusItem` with `NSMenu`.** Fewer surprises in a plain menu,
  but rows with buttons, notices and copyable commands are awkward in a
  menu.
- **Settings on a tab in the menu bar window.** Tried first; see above.
- **Talking to the HTTP API and `launchctl` directly.** Needs the API key,
  duplicates the watchdog's restart logic and cannot write settings safely.

## Consequences

- CI builds and tests the package on a macOS runner with Xcode 16 or newer.
- The bundle from `menubar/Scripts/bundle.sh` is ad-hoc signed only;
  distributing it needs a Developer ID signature and notarization
  (`menubar/Scripts/SIGNING.md`).
- Without the watchdog there is no socket, so the app shows "not running"
  even when both services are up; the watchdog is part of every install.
- A custom state directory needs `VOICED_CONTROL_SOCKET` or the
  `controlSocketPath` default.
- On a crowded menu bar macOS may hide the icon; relaunching the app does
  not reveal it. Settings stay reachable with ⌘, once the app is active.
