# 0012: Watchdog design

Date: 2026-10-14. Status: accepted. Numbered 0005 when it was written; renumbered
because ADR 0005 is the shared inference and audio primitives record.

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
and hands over its `WatchdogConfig` section plus plain `ServiceSpec` values;
the module itself has no third-party imports at runtime and is testable
without any. Re-implementing the config parser to avoid pydantic at startup was
judged more code than it is worth.

Every `interval_s` (default 15 s) it polls each service's `/health` with
`timeout_s` (5 s) and applies these rules per service, implemented as the
pure function `decide(state, observation, now, policy)`:

| Observation | Rule |
|---|---|
| `ok` | reset the failure counter and the degraded timer; end the startup grace period |
| `unreachable` within `startup_grace_s` (300 s) of the watchdog's start, of its last restart of this service, or of launchd handing the service a new pid, until that process first answers with its model loaded | not a failure: the service is loading its model and has not bound its socket yet |
| `starting` (503, model loading) | tolerated while the service's own `uptime_s` is below `startup_grace_s`; beyond that the load is considered hung and the service is restarted |
| `degraded` (answers, last inference failed) | start a timer; restart once it exceeds `degraded_window_s` (120 s) |
| `stalled`: `/health` is `ok` but the queue has been non-empty for `stall_after_s` (60 s) with no inference completing in that time, or the optional probe timed out | one failure |
| anything while launchd reports the job not loaded (after `voiced stop`) | never a restart; logged once |
| `unreachable` (after the grace period), `error` (non-health answer) | one failure |
| any failure | restart at `failures_before_restart` (3) consecutive failures |

The grace rule for refused connections exists because the server runs the
engine's `load()` in its startup phase, before the socket is bound. During a
cold start (first model download can take minutes) `/health` is not
`starting`, it is connection-refused. Without the rule three refused polls
would SIGKILL a service that is 40 s into loading a model, then again after
every cooldown. The rule keys off the watchdog's own clock, not the
service's, because there is nothing to ask yet. Every poll also reads the
pid from `launchctl print`; a new pid means launchd started the service
(crash restart, out of memory, or `voiced start` after the watchdog came up)
and the new process is loading, so the grace period starts again from that
moment, with no failures counted and no degraded timer running. The first
pid the watchdog sees counts as new too. The pid is read before `/health`,
so a health answer is never credited to a process that launchd started
after it.

The grace period ends at the first answer from the current process with its
model loaded (`ok`, or `model_loaded` true in a `degraded` or stalled
answer; never `starting`), not only when `startup_grace_s` runs out. Once a
process has answered that way it has loaded
its model and bound its socket, so a later refused or timed-out poll is a
hang (a `SIGSTOP`, a wedged GPU call holding the event loop), not a load,
and counts as a failure at once. Without this, a service that hangs shortly
after a restart, or shortly after the watchdog itself starts, would sit out
the rest of the 300 s window before the first failure was counted.
`startup_grace_s` remains the upper bound for a load that never answers.

Restarts are rate limited: a second restart of the same service within
`restart_cooldown_s` (120 s) is deferred and logged as `cooldown`; counters
are kept so it fires right after the cooldown. Every restart and every
non-healthy observation is logged at INFO or WARNING; healthy polls are
DEBUG. As a daemon the watchdog logs to the rotating
`<log_dir>/watchdog.log` only (launchd's own stderr capture is not rotated,
so nothing is sent there); run by hand (`voiced watchdog --once`) it logs
to stderr.

Stall detection needs no extra request: `/health` reports `queue_depth`
(waiting plus running) and `last_inference_at` (last completed inference).
The watchdog notes when it first sees the queue non-empty (`busy_since`) and
restarts that timer whenever `last_inference_at` advances or the queue
empties. A queue that has been busy for `stall_after_s` without a single
completion means the running request never finishes while the process is
otherwise alive, which is exactly how a wedged Metal queue looks. Measuring
from `busy_since` rather than from the last completion matters: after an
idle hour the first request would otherwise look stalled the moment it
arrived.

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

Each step's outcome is in the log line. The watchdog never signals pid 0, 1
or itself, whatever `launchctl print` says.

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
- The off-by-default probe is kept: the queue rule only sees a wedge once a
  request is queued, so a GPU that locks up while idle is found at the first
  real request plus `stall_after_s`. Installations that want to find it
  earlier turn the probe on. The code it adds is small and isolated
  (`default_probe`, `run_probe`, the WAV and multipart helpers).
- The decision rules are unit tested against a fake health endpoint
  (`tests/launchd/test_watchdog.py`); no network or models needed.
- `voiced watchdog --once` runs one poll cycle and prints the decisions,
  which is the quickest way to see what the watchdog thinks of a machine.

## Amendment (ADR 0013)

The watchdog process also serves the local control socket. Its loop runs on
asyncio (`Watchdog.run_async`, each tick in a background thread) and a tick
holds `Watchdog.lock` only to decide and restart, not while observing. The
process now imports `voiced.settings_store` (pydantic, tomlkit) and
`voiced.control` besides the standard library; it still imports no engine
code, FastAPI or numpy: the engine catalogue is read in a child process, and
the CLI imports the installer and `doctor` only in the commands that need
them. A test enforces this.

## Amendment (ADR 0017)

Before each poll the watchdog checks that its own environment is still the
installed one. Two things say it is not: a file it runs from
(`sys.prefix`, `sys.base_prefix`, `sys.executable`, resolved at start) was
deleted or replaced (an old keg or Python removed, a keg rebuilt by
`brew reinstall`, an old environment pruned), or `~/.voiced/bin/voiced`
was repointed (`voiced install`, the app). Then it restarts both services
and exits, and launchd starts it again from the new environment; while
Homebrew still holds the lock of one of those formulae it waits, without
restarting anything. Details, and why it does not follow Homebrew's `opt`
link, are in [ADR 0017](0017-homebrew-packaging.md#the-install-layout).

## Amendment (ADR 0033)

Two health statuses and one observation join the table. `idle` (the model
unloaded after `idle_unload_s` without a request) and `loading` (loading it
again for a request that waits) are healthy, as `ok`: they reset the
counters and end the startup grace. A load that hangs holds the queue, so
the stall rule catches it as it catches a wedged inference. A service with
`idle_unload_s` set gets no probe: each probe is a request, which would
load the model and restart its idle time. `off` is a service
switched off whose parked process is the pid launchd runs (its pid is in
`<state dir>/<service>.off`): never polled, never restarted, never a
failure, no grace, reported once per process. Details in
[ADR 0033](0033-model-residency.md).

## Amendment (ADR 0035)

The watchdog and the services are LaunchAgents in the user's domain,
`gui/<uid>`. The restart is unchanged, SIGKILL then `launchctl kickstart
gui/<uid>/<label>`; `kickstart -k` is no longer denied, but one restart
path is kept.
