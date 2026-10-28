# 0035: LaunchAgents, no administrator password

Date: 2026-10-23. Status: accepted; amended 2026-10-28. Supersedes 0004; amends 0003, 0006,
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
anyway, and that unlock logs the unlocking account in. As LaunchAgents in the user's own
domain the services need no root at any point: install, update, repair,
start, stop, restart, uninstall and the Homebrew cask's `zap`. Handy, the
model for the app (ADR 0018), works this way.

What that costs, plainly: the services run while that user is logged in.

| Situation | Services |
|---|---|
| FileVault on, reboot | start when the user unlocks the disk at boot: the unlock logs in the account that unlocked it, so on a Mac with several accounts, only if it was this one |
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
  start`, which bootstraps it again, and returns once launchd has let go
  of the job (`wait_unloaded`): `bootout` returns while launchd still tears
  it down, and a `start` straight after would act on the dying job. No
  `sudo` line.
- **Never as root.** `voiced install`, `start`, `stop`, `restart`,
  `status`, `uninstall` and `doctor` refuse to run with `sudo`: launchctl
  would address root's own domain, `gui/0`, so `sudo voiced stop` would
  find nothing loaded and leave the services running.
- **No GUI session, no agents.** Over SSH with nobody logged in at the Mac
  there is no `gui/<uid>`; the `agents` step says "log in on the Mac first".
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
incoming connections?" question, so Home Assistant on the LAN would have
been blocked silently. Adding the exception needs root
(`socketfilterfw --add`). The services now start in the user's session,
where macOS asks that question itself, at the Mac, when a service starts
listening on the network, and asks again whenever the Python changes: an
update can install a newer interpreter (a `--source` build asks uv for
`3.12`, which may resolve to a newer patch release), and an exception, the
privileged script's or the user's answer, names a file. Until someone
answers at the Mac, devices on the network are blocked, which on an
unattended server means until someone comes by.

So voiced adds nothing and says so. With `bind` on the network and the
firewall on, `voiced install` ends with that note and `voiced doctor` warns
with it: macOS asks when the services start listening and after an update
that changes the Python, and the alternative is to allow the Python in
System Settings > Network > Firewall > Options. The firewall's own list is
not read: `socketfilterfw --listapps` is a display format, and
`--getappblocked` answers "permitted" for any path it does not know. The
README's LAN section and docs/operations.md say the same.

### Bonjour from an agent: Local Network privacy

Apple's TN3179: macOS's Local Network privacy covers LaunchAgents, where
daemons were exempt. python-zeroconf, which advertises the Wyoming
listeners for Home Assistant's discovery, sends its own multicast.
*Measured* on an M1 Max (macOS 15.7.4) with an agent under a test label
prefix, `bind = "0.0.0.0"` and `zeroconf = true`: every send on the
network interface failed with `[Errno 65] No route to host` in the service
log, which then said "advertising" anyway, and `dns-sd -B _wyoming._tcp`
saw voiced's two instances on the loopback interface only. The same
advertisement from a process in a terminal reached the network interface.
No answer was given at the Mac during the test, so whether macOS showed a
Local Network prompt for the agent's Python, and whether allowing it in
System Settings > Privacy & Security > Local Network lifts the block, is
still open. Incoming connections are not affected: Local Network privacy
governs what the process sends, and the HTTP and Wyoming listeners answer
clients as before. Nor are the agent's own connections to the Mac's
address: *measured* with `bind` set to the Mac's LAN address and the
watchdog polling it every 2 s for 40 s, every poll answered and the
watchdog restarted nothing.

So with `bind` on the network and `zeroconf` on, `voiced doctor` browses
for `_wyoming._tcp` with `dns-sd` and passes only when voiced's instances
are seen on an interface other than loopback; otherwise it warns that Home
Assistant may not discover voiced and points to adding the Wyoming
integrations by this Mac's address and port (docs/home-assistant.md),
which works either way. A follow-up, to be tried on a real LAN: advertise
through mDNSResponder (`DNSServiceRegister` through ctypes, or a `dns-sd -R`
child process) instead of python-zeroconf's own multicast, since the
system's responder is not a process of the agent.

### Migration from the LaunchDaemons

Existing installs have daemons in `/Library/LaunchDaemons` that only root
can remove, and the agents must not run beside them: both would bind the
same ports. Stopping the daemons needs root too, so the order is fixed:
one prompt removes the daemons, then the agents install with none.

- **Found by** the three default labels, the only ones the old root step
  installed (ADR 0003), and this install's own: by plist, and by
  `launchctl print system/<label>`, which needs no root, so a daemon still
  loaded after its plist was deleted counts too
  (`voiced.launchd.control.legacy_daemons`; the app checks the same labels
  the same two ways). And the `ports` step takes as voiced's own only the
  processes launchd runs for this install's agents, so an old daemon
  running the same entry point is a holder like any other, rather than
  agents that exit with code 3 every 10 s.
- `voiced install` checks first, before it writes anything (dry run
  included): if one is found, it stops with one message
  (`legacy_message`, which `start`, `stop`, `uninstall`, `status` and
  `doctor` print too) that names the line that removes them and asks to
  run `voiced install` again afterwards:

  ```sh
  sudo /bin/sh -c '/bin/launchctl bootout system/<label> 2>/dev/null; ...; /bin/rm -f /Library/LaunchDaemons/<label>.plist ...'
  ```

  Daemons of another account are not offered for removal; that account
  removes them. `voiced doctor` fails "Old LaunchDaemons" with the same
  message.
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
- **The app**, in Install, Update, Repair and Roll back, when it finds
  such daemons (`InstallStages.hasLegacyDaemons`), does everything that
  can fail first: it builds the new environment and runs
  `voiced install --prepare`, which checks the engines and downloads the
  models and changes nothing that runs (no ports step, since the daemons
  hold the ports, no settings, link, manifest or agents). Only then it
  shows "Remove the old background services", whose button runs the
  removal behind one administrator prompt, worded "voiced needs an
  administrator to remove the old background services (the last time a
  password is needed)", and then the whole `voiced install`, which finds
  the environment and the models in place and so is left with the ports,
  the switch and the agents. Daemons still found after the prompt are a
  failure, not another prompt. Cancelling leaves the daemon install
  untouched and running the old environment, and the screen says the
  services keep running the old way until the prompt is accepted.
  Uninstall of such an install asks the same prompt first.
- **The cask.** The first `brew upgrade --cask voiced` from 0.4.0 or
  earlier still runs that version's own `uninstall` stanza, with sudo,
  which removes the daemons (ADR 0017's 2026-10-23 amendment); the new app then finds no
  daemons and its Update or Repair installs the agents without a prompt.
- **The formula.** `brew upgrade voiced` replaces the keg; the old daemons
  run the new code until `voiced install` has replaced them (the caveats
  say so). Meanwhile the new watchdog looks for its services in the user's
  domain and cannot restart a hung one.

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

The same run installed, restarted (`kickstart -k`, `voiced restart all`),
stopped, started, reinstalled and uninstalled agents under a test label
prefix as the user, with no password asked at any step.

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
  automatic login, or a person to unlock FileVault at boot (as the account
  that installed voiced, on a Mac with several).
- The 233-line root-run script, the path-ownership and ACL checks for
  it, the app's check and approval phases and the firewall step are gone.
- The plists follow the install's layout, so a harness can install real
  agents beside the real ones under another label prefix.
- Open items, for a real LAN install: the firewall's question for the
  agent's Python with a device connecting; whether a Local Network prompt
  appears for the agent and allowing it lets Bonjour reach the network.
  And a follow-up: the services open their own stdout and stderr
  (`VOICED_STDIO_LOG`, ADR 0006) only because launchd opened
  `StandardOutPath` and `StandardErrorPath` as root for a daemon; for an
  agent launchd could open them as the user, so those keys could replace
  that code.

## Amendment (2026-10-28)

The order of the checks. The session check ran last, in the `agents` step:
a 0.4.0 to 0.5.0 migration over SSH with nobody at the Mac had the daemons
removed and the environment built before the install stopped, with the
services down. `voiced install` now checks the session first, before the
old daemons' refusal and before any step, and stops with nothing changed;
a dry run lists the steps, then refuses the same way. The `agents` step
checks again, for a logout during the install, and says to run
`voiced install` again. `--prepare` loads no agent and does not check; the
install that completes it does. One function decides
(`control.session_refusal`), and `voiced doctor` reports it as "Login
session". The migration message says the install after the removal needs
a login at the Mac.
