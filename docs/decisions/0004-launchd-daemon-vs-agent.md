# 0004: LaunchDaemons, not LaunchAgents

Date: 2026-10-14. Status: superseded by 0035 (2026-10-23): the services are
LaunchAgents that run while their user is logged in, and need no administrator.

## Context

The services must start at boot without anyone logging in to the GUI and
must survive logouts. On macOS that means a LaunchDaemon in
`/Library/LaunchDaemons` (system domain, runs before any user session). A
LaunchAgent would only start once its user logs in.

The open question was whether MLX can use Metal from a daemon context. GPU
access on macOS is tied to the window server in some configurations, and
"it works in my terminal" says nothing about a process whose parent is pid 1
and that has no Aqua session. This had to be measured, not assumed.

## Evidence

A throwaway one-shot LaunchDaemon (`UserName` set to the installing user,
`RunAtLoad` true, `KeepAlive` false, `ProcessType Interactive`) ran
`voiced.launchd.metal_probe` on an M1 Max (macOS 15.7.4, MLX
0.32.3, Python 3.12). The probe imports `mlx.core`, multiplies two 1024x1024
random matrices and records the process context. Result:

| Field | Value |
|---|---|
| `launchctl managername` (inside the process) | `System` |
| parent pid | 1 |
| uid / euid | 501 (the installing user, not root) |
| `mx.default_device()` | `Device(gpu, 0)` |
| `mx.metal.is_available()` | true |
| matmul result finite | true |
| matmul wall time | 21 ms (cold, including kernel compilation) |

The same probe from an interactive shell (`managername` `Background`)
reported the same device. So Metal works from a LaunchDaemon that runs as a
normal user, with no GUI session, on this hardware and OS. Other GPU
inference servers (Ollama, for one) ship as LaunchDaemons on macOS, which
agrees with this result.

The probe is kept in the package (`voiced doctor --metal-probe` writes a
self-cleaning script with the probe's plist inside it and prints the `sudo`
command that runs it) so the check can be repeated on any machine. `voiced doctor` accepts the recorded result only if it was
produced under launchd's system manager (`managername` `System`, parent pid
1) on the macOS version that is running now; a probe run from a shell, or
left over from before an OS upgrade, does not count.

Also measured while the probe daemon was loaded, as the unprivileged user:

| Command | Outcome |
|---|---|
| `launchctl print system/<label>` | works |
| `launchctl kickstart system/<label>` | works (starts the job) |
| `launchctl kickstart -k system/<label>` | denied while a process is running ("Operation not permitted"); succeeds when nothing is running, where it is the same as plain `kickstart` |
| `launchctl bootout system/<label>` | denied |

These shape the watchdog (ADR 0012): it never relies on `kickstart -k`. They
also shape the CLI: `start` can kickstart a loaded service without root, but
`stop` (bootout) and first-time `bootstrap` are printed as `sudo` commands
for the user.

## Decision

Both services and the watchdog are LaunchDaemons with `UserName` set to the
installing user. The plists set `RunAtLoad`, `KeepAlive`, `ThrottleInterval
10`, `ProcessType Interactive` (so the scheduler does not treat inference as
background work), `WorkingDirectory` = the user's home, and an explicit
environment: `HOME`, `PATH` (venv first, then Homebrew, then system),
`VOICED_CONFIG` (so the daemon reads the same file as the CLI regardless of
how launchd resolves `~`), `PYTHONUNBUFFERED=1` and `VOICED_STDIO_LOG`;
everything else, `HF_HOME` included, the daemon reads from that config file
(ADR 0018). Stdout and stderr go to `<log_dir>/<service>.stdout.log` and
`.stderr.log`, opened by the process itself rather than through
`StandardOutPath`/`StandardErrorPath` (ADR 0006, Logs); the application log
is `<log_dir>/<service>.log`.

## Consequences

- Installing and removing the plists needs root. The installer never runs
  `sudo` itself. One script does it for the CLI and the menu bar app alike
  (ADR 0018): `voiced install` prints `sudo /bin/bash <environment>/.../
  voiced/launchd/voiced-privileged.sh install <user>`, and the script
  renders the plists as root from the user name alone. Nothing the user's
  account writes is staged for root to copy.
- With FileVault on, a reboot waits at the pre-boot unlock screen; the
  daemons start as soon as the disk is unlocked, no GUI login required.
  `voiced doctor` warns about this.
- If a future macOS breaks Metal for daemons, the fallback is a LaunchAgent
  with the same plist content installed under `~/Library/LaunchAgents` and
  `launchctl bootstrap gui/<uid>`. Only the privileged script's
  destination, its `UserName` key and the launchctl domain would change.
