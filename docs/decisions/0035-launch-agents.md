# 0035: LaunchAgents, no administrator password

Date: 2026-10-23. Status: accepted. Supersedes 0004; amends 0003, 0006,
0012, 0014, 0017, 0018 and 0033.

## Context

ADR 0004 chose LaunchDaemons so the services would start at boot before
anyone logs in and survive a logout. That choice is the only reason voiced
needs an administrator: `/Library/LaunchDaemons` and the system domain
belong to root, so installing, repairing, removing and even stopping the
services took a password (ADR 0018's privileged script, its one prompt, the
CLI's printed `sudo` line, the cask's sudo uninstall).

The owner decided on 2026-10-23 that the services run while their user is
logged in, and no longer before: most Macs have FileVault on, and with
FileVault a daemon does not start before someone unlocks the disk at boot
anyway, and that unlock logs the user in. As LaunchAgents in the user's own
domain the services need no root at any point: install, update, repair,
start, stop, restart, uninstall and the Homebrew cask's `zap`. Handy, the
model for the app (ADR 0018), works this way.

What that costs, plainly: the services run while that user is logged in.

| Situation | Services |
|---|---|
| FileVault on, reboot | start when the disk is unlocked at boot: the unlock logs the user in |
| FileVault off, automatic login on, reboot | start at the automatic login |
| FileVault off, no automatic login, reboot | wait at the login window until the user logs in |
| User logs out | stop (launchd ends the user's session, and its agents with it) |
| User logs in | start (`RunAtLoad`) |
| Screen locked, display asleep, another user switched in (fast user switching) | keep running: the user is still logged in |

A Mac used as a server keeps its user logged in, or has automatic login on
(which macOS offers only with FileVault off). `voiced doctor` says which of
the rows applies ("After a reboot").

## Decision

### Where and how

- **Domain** `gui/<uid>`, the logged-in user's. Every `launchctl` verb is
  run as the user: `bootstrap gui/<uid> <plist>`, `bootout
  gui/<uid>/<label>`, `kickstart gui/<uid>/<label>`, `print
  gui/<uid>/<label>` (`voiced.launchd.control`).
- **Plists** in `~/Library/LaunchAgents/<label>.plist`, mode 0644, owned
  by the user: `io.github.julianbeaulieu.voiced.{stt,tts,watchdog}.plist`
  by default. launchd loads that directory into the user's domain at every
  login.
- **Keys** kept from the daemon plists: `Label`, `ProgramArguments`
  (`<state dir>/bin/voiced` plus `serve <service>` or `watchdog`),
  `RunAtLoad`, `KeepAlive` `{PathState: {<entry point>: true}}` (ADR 0017),
  `ThrottleInterval` 10, `ProcessType Interactive`, `WorkingDirectory` the
  home, `AssociatedBundleIdentifiers` the app (Login Items lists agents
  too), and the explicit environment: `HOME`, `PATH` (the state
  directory's `bin` first), `VOICED_CONFIG`, `PYTHONUNBUFFERED` and
  `VOICED_STDIO_LOG`. Still no `StandardOutPath`/`StandardErrorPath`: the
  services open their own logs (ADR 0006). **`UserName` goes**: an agent
  runs as the user whose domain it is in.
- **`voiced install` renders and loads them itself** as its last step,
  `agents` (`install_agents`): per label, if the file on disk is byte for
  byte what it would write and the label is loaded, nothing happens (the
  watchdog moves running services to a new environment, ADR 0017);
  otherwise boot out, wait until the label is gone, write the plist
  atomically, bootstrap. A label that fails to load is named and does not
  stop the others. This is the plain file compare that replaces
  `voiced-privileged check`, done where the plists are written.
- Since no root reads anything the user wrote, the plists follow the
  install's own layout: a `--state-dir`, a `VOICED_CONFIG` or a `[launchd]
  label_prefix` gets agents of its own. A test harness can run beside the
  real install under another prefix (ADR 0003), which the root step could
  not offer.

### What the other records said that changes

- **Watchdog (ADR 0012).** The restart stays SIGKILL of the pid, then
  `launchctl kickstart`, now plainly in the watchdog's own domain.
  `kickstart -k` would work as the user too (measured below), but the one
  restart path already works, and SIGKILL first is what a wedged Metal
  process needs.
- **A service switched off (ADR 0033)** still parks its process; the
  agent stays loaded. Unloading would now be possible without root, but
  parking keeps on and off one restart apart, through the watchdog, with
  nothing to load again. The parked check compares the marker with the pid
  `launchctl print gui/<uid>/<label>` reports.
- **`voiced stop`** boots the agent out until the next login or `voiced
  start`, which bootstraps it again. No `sudo` line.
- **The privileged script (ADR 0018)** is gone: no `install`, `check` or
  `uninstall` mode, no root rendering, no lock in `/var/run`, no path check
  of who could write the script (`foreign_writer`), no `osascript` in the
  normal flow. The app's Install, Update, Repair, Roll back and Uninstall
  run without a prompt. The bundle self-check stays, for the one prompt
  below.
- **The cask's `zap` (ADR 0017)** boots the three agents out of
  `gui/<uid>` by their plists and trashes the plists, as the user. Not
  `launchctl:`: Homebrew's `launchctl:` also looks for each label in the
  system domain with sudo whenever the account may use it, which asks for a
  password with nothing there. The uid and home are read by the shell when
  `zap` runs, not when the cask is loaded. An install still on
  LaunchDaemons is not zapped; it migrates first, or is removed with the
  removal line below.
- **One install per account, not per Mac.** Agents are per user, so the
  "already installed for another account" refusal applies only to another
  account's old LaunchDaemons. Two accounts logged in at once with the same
  ports collide, and the `ports` step of the second one's install names
  the holder ("another account or the system").

### The firewall exception (ADR 0018 item 8): dropped

It existed because the services' Python is signed ad hoc and, running
before login, nobody could answer the application firewall's "accept
incoming connections?" prompt, so Home Assistant on the LAN would have been
blocked silently. Adding the exception needs root
(`socketfilterfw --add`). The services now start in the user's session,
where macOS shows that prompt itself the first time a device connects, and
the logged-in user answers it once. So voiced does nothing, and says so:
with `bind = lan` and the firewall on, `voiced doctor` reads the
firewall's list as the user (`socketfilterfw --listapps`) and passes when
the Python is allowed, fails when it is blocked, and otherwise warns that
macOS will ask, with "choose Allow". The README's LAN section says the
same. An exception the privileged script added before stays in the
firewall's list; it names the same Python and keeps working.

### Migration from the LaunchDaemons

Existing installs have daemons in `/Library/LaunchDaemons` that only root
can remove, and the agents must not run beside them: both would bind the
same ports. Stopping the daemons needs root too, so the order is fixed:
one prompt removes the daemons, then the agents install with none.

- `voiced install` checks first, before it writes anything (dry run
  included): if a daemon plist for one of its labels exists, it stops
  with the one line that removes them, and asks to run `voiced install`
  again afterwards:

  ```sh
  sudo /bin/sh -c '/bin/launchctl bootout system/<label> 2>/dev/null; ...; /bin/rm -f /Library/LaunchDaemons/<label>.plist ...'
  ```

  Daemons of another account are not offered for removal; that account
  removes them. `voiced start`, `stop`, `uninstall` and `status` print the
  same line while daemons are there, and `voiced doctor` fails "Old
  LaunchDaemons" with it.
- The removal is fixed commands by absolute path with no input at all:
  `launchctl bootout` of the three labels and `rm -f` of their plists. So
  the privileged script is not kept for it: a script would only add input
  (the user name) that this step does not need, and the CLI would again
  print a file the user's account can write for root to run. Python has
  the command in `voiced.launchd.control.legacy_removal_command`, the app
  the same text in `LegacyDaemons.swift`; a test keeps them equal. Both may
  be deleted in 0.7.0, two minor releases after the first one without
  daemons, after which an install from 0.4.0 or earlier migrates by hand
  (docs/operations.md keeps the commands).
- **The app** checks before it runs `voiced install` in Install, Update,
  Repair and Roll back (`InstallProbe.hasLegacyDaemons`): it builds the new
  environment first, then shows "Remove the old background services", whose
  button runs the removal behind one administrator prompt, worded
  "voiced needs an administrator to remove the old background services
  (the last time a password is needed)". Then `voiced install` runs and
  loads the agents. Cancelling leaves the daemon install untouched and
  running the old environment, and the screen says the services keep
  running the old way until the prompt is accepted. Uninstall of such an
  install asks the same prompt first.
- **The cask.** The first `brew upgrade --cask voiced` from 0.4.0 or
  earlier still runs that version's own `uninstall` stanza, with sudo,
  which removes the daemons (ADR 0017's 2026-10-23 amendment); the new app then finds no
  daemons and its Update or Repair installs the agents without a prompt.
- **The formula.** `brew upgrade voiced` replaces the keg; the old daemons
  run the new code until `voiced install` has replaced them (the caveats
  say so). Meanwhile the new watchdog looks for its services in the user's
  domain, finds none, and does not restart a hung service.

### The Metal check

ADR 0004's probe, `voiced.launchd.metal_probe`, run once as a LaunchAgent
the way 0004 ran it as a daemon (`RunAtLoad`, `KeepAlive` false, `ProcessType
Interactive`, the services' environment), bootstrapped into `gui/501` from a
plist in a temporary directory and booted out after its result.
`voiced doctor --metal-probe` now does exactly this itself, without `sudo`,
and `voiced doctor` accepts a probe result only from launchd's `Aqua`
manager with pid 1 as parent. *Measured* on an M1 Max, macOS 15.7.4, MLX
0.32.3, Python 3.12.12:

| Field | Value |
|---|---|
| `launchctl managername` (inside the process) | `Aqua` |
| parent pid | 1 |
| uid / euid | 501 / 501 |
| `mx.default_device()` | `Device(gpu, 0)` |
| `mx.metal.is_available()` | true |
| matmul result finite | true |
| matmul wall time | 22 ms (cold, including kernel compilation) |

As expected: an agent has the user's Aqua session, which a daemon lacked
and still had Metal.

*Measured* in the same run, as the user, with agents under a test label
prefix in a scratch state directory, fake engines on spare ports (nothing
of the installed voiced touched): `voiced install` loaded the three agents
(`type = LaunchAgent`, `domain = gui/501`); both `/health` answered `ok`;
`launchctl kickstart -k gui/501/<label>` exited 0 and launchd started a new
pid that answered again; `voiced restart all`, `voiced stop stt` then
`voiced start stt`, and a second `voiced install` (every agent "up to
date", every pid unchanged) worked; `voiced uninstall --yes` booted all
three out and deleted their plists. No step asked for a password.

## Alternatives considered

- **Keep the LaunchDaemons.** Every install, update that changes a plist,
  repair and uninstall needs an administrator, and the start-before-login
  they buy does not happen with FileVault on, which most Macs have.
- **Both: agents, with a daemon for those who want boot without login.**
  Two install paths, two plist renderers, the privileged script kept, and
  every launchctl call doubled for a case the owner chose not to serve.
- **`SMAppService.agent(plistName:)`** from the app. The plist would be
  sealed in the bundle, so the per-user environment (`HOME`,
  `VOICED_CONFIG`, the state directory) could not be in it, and the CLI and
  Homebrew installs would need another path anyway. `voiced install`
  writing plain plists serves all three.
- **Keeping `voiced-privileged.sh` with only its `uninstall` mode** for the
  migration. It takes a user name and runs from a file the user's account
  can write; the fixed removal needs neither.

## Consequences

- No step of voiced needs an administrator, except, once per Mac that ran
  0.4.0 or earlier, the removal of the old daemons.
- The services stop at logout and start at login. Unattended reboots need
  FileVault (its unlock is the login) or automatic login.
- The 233-line root-run script, the path-ownership and ACL checks for
  it, the app's check and approval phases and the firewall step are gone.
- The plists follow the install's layout, so a harness can install real
  agents beside the real ones under another label prefix.
- Open items: the firewall's own prompt with a LAN client on a real
  install, and whether advertising over Bonjour from an agent (`zeroconf`)
  brings up macOS 15's Local Network prompt, which ADR 0018 noted does not
  apply to daemons; both on the Studio during the migration.
