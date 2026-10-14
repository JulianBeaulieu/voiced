# 0005: Watchdog design

Date: 2026-10-14. Status: accepted.

## Context

`KeepAlive` restarts a process that exits, but MLX can wedge without
exiting: the process stays alive, the GPU queue never drains, and `/health`
either times out or keeps answering while inference stalls. Something
outside the process has to notice and restart it. That something must be
boring: no model, no pydantic, fast to start, impossible to wedge.

## Decision

`voiced watchdog` is a third LaunchDaemon. `voiced.watchdog` imports only
the standard library (`urllib`, `json`, `logging`, `subprocess`). The CLI
turns `Settings` into plain values (`Policy`, `ServiceSpec`) before handing
them over, so the module stays importable without any dependency.

Every `interval_s` (default 15 s) it polls each service's `/health` with
`timeout_s` (5 s) and applies these rules per service, implemented as the
pure function `decide(state, observation, now, policy)`:

| Observation | Rule |
|---|---|
| `ok` | reset the failure counter and the degraded timer |
| `starting` (503, model loading) | tolerated while the service's own `uptime_s` is below `startup_grace_s` (300 s); beyond that the load is considered hung and the service is restarted |
| `degraded` (answers, last inference failed) | start a timer; restart once it exceeds `degraded_window_s` (120 s) |
| `unreachable` (timeout, refused), `error` (non-health answer), `stalled` (probe timeout) | one failure; restart at `failures_before_restart` (3) consecutive failures |

Restarts are rate limited: a second restart of the same service within
`restart_cooldown_s` (120 s) is deferred and logged as `cooldown`; counters
are kept so it fires right after the cooldown. Every restart and every
non-healthy observation is logged at INFO or WARNING; healthy polls are
DEBUG. Logs go to `<log_dir>/watchdog.log` (rotating) and stderr.

Optional probe (`[watchdog] probe = true`, off by default): when `/health`
is `ok`, the watchdog also sends a tiny inference request (`POST
/v1/audio/speech` with the word "ok"; `POST /v1/audio/transcriptions` with
0.2 s of silence) and treats a timeout of `probe_timeout_s` (20 s) as
`stalled`. Any HTTP answer, including 4xx or 501, means the service is not
stalled and is ignored. This catches the "health answers but the GPU is
stuck" case at the cost of one small inference per poll.

### How a restart happens

`launchctl kickstart -k system/<label>` is the intended mechanism, and the
watchdog tries it first. Measured on macOS 15 (ADR 0004): for a non-root
caller, `-k` is denied while the target has a running process, plain
`kickstart` is allowed. The watchdog runs as the installing user, not root,
on purpose (a root daemon executing Python from a user-writable venv is a
privilege escalation path). So the fallback is:

1. `launchctl print system/<label>` to find the pid;
2. `os.kill(pid, SIGKILL)`: permitted because the service runs under the
   same `UserName`; SIGKILL because a wedged Metal process may not react to
   SIGTERM;
3. `launchctl kickstart system/<label>` so the service comes back now
   instead of after `ThrottleInterval`.

Each step's outcome is in the log line.

## Alternatives considered

- **Root watchdog** so `kickstart -k` and `bootout` always work. Rejected:
  it would run code from `~/.voiced` as root.
- **In-process watchdog thread** in each service. Rejected: a wedged Metal
  call can block the whole process, including the thread meant to detect it.
- **Health check inside KeepAlive** (`KeepAlive` with `PathState` or
  `OtherJobEnabled`). launchd has no HTTP health check; this cannot express
  "answering but stalled".

## Consequences

- A service that crash-loops is restarted by launchd (`KeepAlive`,
  `ThrottleInterval 10`), not by the watchdog; the watchdog only steps in for
  hangs, stuck loads and persistent degradation.
- The decision rules are unit tested against a fake health endpoint
  (`tests/launchd/test_watchdog.py`); no network or models needed.
- `voiced watchdog --once` runs one poll cycle and prints the decisions,
  which is the quickest way to see what the watchdog thinks of a machine.
