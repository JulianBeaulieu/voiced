# 0004: LaunchDaemons, not LaunchAgents

Date: 2026-10-14. Status: accepted.

## Context

The services must start at boot without anyone logging in to the GUI and
must survive logouts. On macOS that means a LaunchDaemon in
`/Library/LaunchDaemons` (system domain, runs before any user session). A
LaunchAgent would only start once its user logs in.

The open question was whether MLX can use Metal from a daemon context. GPU
access on macOS is tied to the window server in some configurations, and
"it works in my terminal" says nothing about a process whose parent is pid 1
and that has no Aqua session. The handoff said: test it, do not assume.

## Evidence

A throwaway one-shot LaunchDaemon (`UserName` set to the installing user,
`RunAtLoad` true, `KeepAlive` false, `ProcessType Interactive`) ran
`voiced.launchd.metal_probe` on the development machine (MacBook Pro M1 Max,
macOS 15.7.4, MLX 0.32.3, Python 3.12). The probe imports `mlx.core`,
multiplies two 1024x1024 random matrices and records the process context.
Result:

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
normal user, with no GUI session, on this hardware and OS. Supporting prior
art: Ollama runs as a LaunchDaemon with GPU acceleration on the author's
other Mac.

The probe is kept in the package (`voiced doctor --metal-probe` stages the
plist and prints a self-cleaning sudo script) so the check can be repeated on
any machine, including the deployment target with a newer macOS.

Also measured while the probe daemon was loaded, as the unprivileged user:

| Command | Outcome |
|---|---|
| `launchctl print system/<label>` | works |
| `launchctl kickstart system/<label>` | works (starts the job) |
| `launchctl kickstart -k system/<label>` | denied while a process is running ("Operation not permitted"); succeeds when nothing is running |
| `launchctl bootout system/<label>` | denied |

These shape the watchdog (ADR 0005) and the CLI: `start` can kickstart a
loaded service without root, but `stop` (bootout) and first-time
`bootstrap` are printed as `sudo` commands for the user.

## Decision

Both services and the watchdog are LaunchDaemons with `UserName` set to the
installing user. The plists set `RunAtLoad`, `KeepAlive`, `ThrottleInterval
10`, `ProcessType Interactive` (so the scheduler does not treat inference as
background work), `WorkingDirectory` = the user's home, and an explicit
environment: `HOME`, `PATH` (venv first, then Homebrew, then system),
`VOICED_CONFIG` (so the daemon reads the same file as the CLI regardless of
how launchd resolves `~`), `PYTHONUNBUFFERED=1`, and `HF_HOME` when
configured. Stdout and stderr go to `<log_dir>/<service>.stdout.log` and
`.stderr.log`; the application log is `<log_dir>/<service>.log`.

## Consequences

- Installing and removing the plists needs root. The installer never runs
  `sudo` itself; it writes the plists to a staging directory and prints a
  script (`install-daemons.sh`) for the user to review and run.
- With FileVault on, a reboot waits at the pre-boot unlock screen; the
  daemons start as soon as the disk is unlocked, no GUI login required.
  `voiced doctor` warns about this.
- If a future macOS breaks Metal for daemons, the fallback is a LaunchAgent
  with the same plist content installed under `~/Library/LaunchAgents` and
  `launchctl bootstrap gui/<uid>`. The renderer already takes every field
  needed; only the install script and the target directory would change.
