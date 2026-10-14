# 0005: Watchdog design

Date: 2026-10-14. Status: accepted.

## Context

`KeepAlive` restarts a process that exits, but MLX can wedge without
exiting: the process stays alive, the GPU queue never drains, and `/health`
either times out or keeps answering while inference stalls. Something
outside the process has to notice and restart it. That something must be
boring: no model, no pydantic, fast to start, impossible to wedge.

## Decision

`voiced watchdog` is a third LaunchDaemon. The `voiced.watchdog` module and
the `voiced.launchd.control` helpers it uses import only the standard library
(`urllib`, `json`, `logging`, `subprocess`). The process is started through
the normal `voiced` entry point, which loads the TOML config with pydantic
and converts it to plain values (`Policy`, `ServiceSpec`) before handing
over; the module itself has no third-party imports and is testable without
any. Re-implementing the config parser to avoid pydantic at startup was
judged more code than it is worth.

Every `interval_s` (default 15 s) it polls each service's `/health` with
`timeout_s` (5 s) and applies these rules per service, implemented as the
pure function `decide(state, observation, now, policy)`:

| Observation | Rule |
|---|---|
| `ok` | reset the failure counter and the degraded timer |
| `unreachable` within `startup_grace_s` (300 s) of the watchdog's start or of its last restart of this service | not a failure: the service is loading its model and has not bound its socket yet |
| `starting` (503, model loading) | tolerated while the service's own `uptime_s` is below `startup_grace_s`; beyond that the load is considered hung and the service is restarted |
| `degraded` (answers, last inference failed) | start a timer; restart once it exceeds `degraded_window_s` (120 s) |
| `stalled`: `/health` is `ok` but `queue_depth > 0` and no inference has completed for `stall_after_s` (60 s), or the optional probe timed out | one failure |
| `unreachable` (after the grace period), `error` (non-health answer) | one failure |
| any failure | restart at `failures_before_restart` (3) consecutive failures |

The grace rule for refused connections exists because the server runs the
engine's `load()` in its startup phase, before the socket is bound. During a
cold start (first model download can take minutes) `/health` is not
`starting`, it is connection-refused. Without the rule three refused polls
would SIGKILL a service that is 40 s into loading a model, then again after
every cooldown. The rule keys off the watchdog's own clock, not the
service's, because there is nothing to ask yet.

Restarts are rate limited: a second restart of the same service within
`restart_cooldown_s` (120 s) is deferred and logged as `cooldown`; counters
are kept so it fires right after the cooldown. Every restart and every
non-healthy observation is logged at INFO or WARNING; healthy polls are
DEBUG. Logs go to `<log_dir>/watchdog.log` (rotating) and stderr.

Stall detection needs no extra request: `/health` reports `queue_depth`
(waiting plus running) and `last_inference_at` (last completed inference).
A non-empty queue whose last completion is older than `stall_after_s` means
the running request never finishes while the process is otherwise alive,
which is exactly how a wedged Metal queue looks. Before anything has
completed, the service's start time stands in for `last_inference_at`.

Optional probe (`[watchdog] probe = true`, off by default): when `/health`
is `ok` and the queue is empty, the watchdog also sends a tiny inference
request (`POST /v1/audio/speech` with the word "ok"; `POST
/v1/audio/transcriptions` with 0.2 s of silence) and treats a timeout of
`probe_timeout_s` (20 s) as `stalled`. Any HTTP answer, including 4xx or
501, means the service is not stalled and is ignored. The probe is skipped
while requests are in flight: a timeout then would just be queueing, and
restarting would kill real work. The probe catches a GPU that wedges while
idle, which the queue rule cannot see.

### How a restart happens

The watchdog runs as the installing user, not root, on purpose: a root
daemon executing Python from a user-writable venv is a privilege escalation
path. Measured on macOS 15 (ADR 0004): for a non-root caller,
`launchctl kickstart -k` is denied while the target has a running process,
and plain `kickstart` is allowed. So there is one restart path, with no
privileged attempt first:

1. `launchctl print system/<label>` to find the pid (if the service is not
   loaded at all, there is nothing to restart and the watchdog says so);
2. `os.kill(pid, SIGKILL)` when there is a pid: permitted because the
   service runs under the same `UserName`; SIGKILL because a wedged Metal
   process may not react to SIGTERM;
3. `launchctl kickstart system/<label>` so the service comes back now
   instead of after `ThrottleInterval`.

Each step's outcome is in the log line. Logging goes to the rotating
`<log_dir>/watchdog.log` when running as a daemon (launchd's own stderr
capture is not rotated, so nothing is sent there) and to stderr when run by
hand.

## Alternatives considered

- **Root watchdog** so `kickstart -k` and `bootout` always work. Rejected:
  it would run code from `~/.voiced` as root.
- **Probe on by default** as the only stall detector. Rejected: it costs an
  inference per poll on every installation and interferes with a busy queue;
  the queue rule above covers the common case for free, and the probe stays
  available for the idle-wedge case.
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
