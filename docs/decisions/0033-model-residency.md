# 0033: Keep a model loaded only while it is wanted

Date: 2026-10-23. Status: accepted.

## Context

Each service loads its model when it starts and keeps it for the life of
the process: Parakeet holds about 1.5 GB, Kokoro about 0.5 to 0.7 GB.
That is right for a Mac that answers Home Assistant all day. It is wrong
for a laptop where voiced speaks a coding assistant's replies a few times
an hour, and wrong for one where another app (Handy, say) already does the
listening, so voiced's Parakeet is a second copy of a model nobody uses.

Until now the only way to free that memory was `voiced stop stt`: it needs
root, `voiced start`, the app's Repair and the next boot undo it, and the
app and `voiced status` then call the service down, not off.

Two settings, per service, cover both cases: unload the model after a
while without requests, and switch a service off.

## Decision

### Measured first

The idle unload is only worth having if the memory goes back to the
system. Measured on an M1 Max (64 GB, macOS 15.7) with a temporary
`HF_HOME`, physical footprint (`proc_pid_rusage`, what Activity Monitor
shows) and MLX's active memory:

Two `voiced serve` processes with `idle_unload_s = 5`, read from
`/metrics.json`:

| Service | Loaded | Idle (unloaded) | First request after idle | Same request warm |
|---|---|---|---|---|
| stt (Parakeet) | 1.51 GB, MLX 1.28 GB | 0.12 GB, MLX 0 | 0.53 s (a 4 s upload) | 0.13 s |
| tts (Kokoro) | 0.47 GB, MLX 0.34 GB | 0.10 GB, MLX 0 | 0.34 s (one sentence, whole) | 0.10 s |

So a reload costs about 0.25 to 0.4 s, with the model's files in the page
cache; after a long idle on a busy Mac they may have to come from the SSD,
which adds a fraction of a second for Parakeet's 2.5 GB. Five
load-unload cycles in one process leave the footprint where one leaves it.
After the first requests the loaded footprint is higher (2.1 GB and
0.7 GB above, MLX's buffer cache), and an unload returns that too.

Every shipped engine, each in its own process, loaded, used once, unloaded
and loaded again (`tests/test_unload_models.py`, the model tier):

| Engine | Before load | Loaded and used | Unloaded | Loaded again |
|---|---|---|---|---|
| parakeet | 0.04 | 1.51 | 0.23 | 1.51 |
| nemotron | 0.04 | 1.42 | 0.15 | 1.43 |
| qwen3-asr | 0.05 | 2.46 | 0.31 | 2.48 |
| whisper | 0.04 | 1.01 | 0.18 | 1.01 |
| moonshine (ONNX, CPU) | 0.04 | 0.29 | 0.13 | 0.12 |
| kokoro | 0.04 | 0.72 | 0.12 | 0.72 |
| soprano | 0.04 | 0.39 | 0.16 | 0.39 |
| pocket | 0.04 | 0.66 | 0.15 | 0.66 |
| chatterbox | 0.04 | 0.89 | 0.19 | 0.91 |
| omnivoice | 0.04 | 2.14 | 0.26 | 2.17 |
| marvis | 0.04 | 1.18 | 0.20 | 1.18 |
| phonon (CPU) | 0.04 | 0.17 | 0.07 | 0.17 |

GB. What stays after an unload, 0.1 to 0.3 GB, is the libraries the load
imported and MLX's compiled kernels, not the model. Moonshine's "loaded
again" is lower than its first load because the footprint counts only
memory the process wrote: its resident set is 0.70 GB after the first
load and 0.71 GB after the reload, and in the reload ONNX Runtime reads
the model through clean pages mapped from the file, which the footprint
leaves out. The unload itself returns 0.16 GB of its 0.29 GB either way
(two runs gave the same numbers). For the MLX engines an unload returns 85
to 90 percent of what loading added (Soprano, whose model is small next
to those libraries, 66 percent).

### The engine contract

`STTEngine` and `TTSEngine` gain `async def unload()`, with a default that
raises `NotImplementedError`; no existing signature changes. It is not
abstract: an abstract method would stop every engine written before it
from being created at all, where the default only keeps such an engine's
model loaded. An engine
drops every reference to its model state (weights, voice tensors, prompts,
tokenizers) on the thread that ran the model, then calls
`voiced.engines.memory.release_memory()`: a garbage collection, then
`mx.clear_cache()`, without which MLX keeps the freed buffers for reuse.
`voiced.engines.memory.unload_on()` does both on the engine's model
thread, so an engine only names what it drops.
`load()` is then called again before any other method. Every shipped
engine implements it; `SentenceTTSEngine` does it once for the TTS engines,
which list what they keep beyond `_model` in `_unload_sync()`. A
third-party engine that does not implement it keeps its model: the service
sees the base class's method, starts no timer, and logs once that
`idle_unload_s` has no effect. Inference paths are
untouched.

### Who loads and unloads

`voiced.residency.Residency`, one per service in its `ServiceContext`,
owns every load and unload. The first load at start is as before
(failures exit the process; a missing model is `missing_model`). Then:

- **When to unload.** A timer task, started only when `idle_unload_s` is
  above 0, unloads the model once `idle_unload_s` has passed since the last
  request ended and the inference queue is empty. Every request goes
  through `inference()`, which marks its end; a request in progress, a
  stream session included, holds the queue, so an unload never runs
  alongside one.
- **Loading again.** `inference()`, once it holds the slot, awaits
  `Residency.ensure_loaded()`, which loads the model if it is unloaded.
  The request waits for that load; it is never refused. That holds for
  HTTP and Wyoming alike, and for a stream that arrives during the load:
  its session opens when the model is there, its audio buffered meanwhile.
  One lock orders loads and unloads, so a request that arrives during an
  unload waits for it and then loads. The reload runs as a task of its own
  that every waiting request shields: two requests share one load, and a
  client that hangs up during it cannot cancel it half way, which would
  leave the model in memory while health says `idle`. The load is not
  counted in the request's latency metrics.
- **A load that fails** fails the request that waited for it (a 500, health
  `degraded`) and the next request tries again; the watchdog's degraded
  rule restarts the service if it stays that way. A model deleted while it
  was unloaded is `missing_model`, as at start.
- **Voices** are taken from the engine at the first load and listed from
  there while the model is unloaded, so Home Assistant's `Describe` and
  `/v1/audio/voices` never load it.

### Health

`/health` gains two statuses, both answered with 200:

| `status` | `model_loaded` | Meaning |
|---|---|---|
| `idle` | false | Unloaded after `idle_unload_s` without a request; the next request loads it |
| `loading` | false | Loading again for a request that waits |

`starting` stays the first load only (503). An error outranks `idle`
(`degraded`). ADR 0012's table gains these rows:

| Observation | Rule |
|---|---|
| `idle`, `loading` | healthy, as `ok`: reset the counters, end the startup grace |
| `loading` with requests queued and no completion for `stall_after_s` | `stalled`, one failure: a load that hangs holds the queue as a wedged inference does |
| `off` (the pid launchd runs is the service's parked process) | never a restart, never a failure, no grace; reported once per process |

The optional probe (`[watchdog] probe = true`) is a real request, so it
would load the model and restart its idle time at every poll: a service
with `idle_unload_s` set gets no probe at all, and the queue rule alone
catches a wedge there. The
status page, `voiced status`, the control channel's `status` (state
`running`, `warm` true, `health.status` saying which) and the app show
loaded, idle and loading.

### Off: the daemon stays, the process parks

A LaunchDaemon that stays unloaded across a reboot needs root:
`launchctl bootout` and `launchctl disable` in the system domain. Today a
settings change in the app restarts the service without root (the
watchdog's SIGKILL and `kickstart`, ADR 0012), so making a switch need an
administrator's password every time is the wrong trade. Instead
`[stt] enabled = false` (or `[tts]`) is read by the service itself:

- `voiced serve` reads it before anything else, configures its log, writes
  its pid to `<state dir>/<service>.off` (`voiced.launchd.parked`) and
  waits for SIGTERM. No engine is built, no port is bound, so nothing is
  advertised over Wyoming or zeroconf. The process costs 29 MB and holds
  no model. A serving process removes the file when it starts.
- **Parked, not hung.** The watchdog, `voiced status` and the control
  channel call a service `off` exactly when the pid in its file is the pid
  launchd runs for its label now. That is a fact about the running
  process, so it cannot drift from the config file: a setting changed
  while the service runs takes effect, and shows, when the service
  restarts, as an engine change does (ADR 0013). A killed parked process
  leaves a file whose pid is gone, which never matches. A parked process
  is never polled, never restarted and never counts toward the startup
  grace or a failure.
- **Switching** is `settings.set stt.enabled=false`, which answers
  `restart_required: ["stt"]`, then `service.restart`, the same path as an
  engine change: no new prompt. The app's switch does both.
- `voiced install`, the app's Repair and Update install and bootstrap the
  daemon as always (the privileged script is unchanged; root still reads
  nothing the user wrote) and say that the service stays off. At boot it
  starts, parks and holds nothing. `voiced start stt` on a service that is
  off loads its daemon as for any service (so turning it on stays a restart
  without root) and says how to turn it on. `voiced status` says
  "off until restarted" when the file already turns it on. The other
  service's status page says it is off, by the same test: its marker's pid
  is the pid `launchctl print` reports for the label. `voiced doctor`
  does not check an off service's model, and `voiced uninstall` removes a
  marker a killed parked process left.
- `voiced stop stt` still boots the daemon out, for whoever wants the
  29 MB process gone too, until the next boot or `voiced start`.

### The settings

`[stt]` and `[tts]` each gain `enabled` (default true) and
`idle_unload_s` (whole seconds, default 0: never, at most a week). Both
are settable over the control channel (`stt.enabled`,
`stt.idle_unload_s`, `tts.enabled`, `tts.idle_unload_s`), so `voiced
settings` and the status page's form have them too, and both need the
service restarted. The app's Speech to Text and Text to Speech panes get a
switch to run the service and a choice of when to unload: never, or
after 1, 5, 15 or 60 minutes unused (a value set in the file is shown as
it is). Either change restarts that service.

The defaults change nothing: both services on, models always loaded. Home
Assistant users are the reason. A voice assistant's first request after a
quiet hour would pay the reload, 0.25 to 0.4 s, on top of the STT
finalize target (ADR 0008) and the 200 ms first audio target (README),
and a Mac that runs voiced for Home Assistant usually has the memory to
spare.

## Alternatives considered

- **Unload the daemon for off** (`bootout` plus `launchctl disable`, from
  the privileged script). Truly nothing runs, but every switch needs root,
  so the app would ask for an administrator's password each time, and the
  root script would need an input beyond the user name. Rejected for a
  29 MB process.
- **Read `enabled` from the config file in the watchdog.** Simpler to
  write, but the file can say off while the old process still serves (and
  must still be supervised), or on while a parked process waits for its
  restart. The pid file says what runs.
- **Exit instead of parking.** `KeepAlive` (a `PathState` on the entry
  point, ADR 0017) restarts a process that exits, every
  `ThrottleInterval`; an exit code cannot say "stay down".
- **Refuse requests while unloaded** (503 with `Retry-After`) and load in
  the background. Clients would need to retry, and Home Assistant does
  not; waiting 0.25 to 0.4 s is better.
- **Unload after every request.** That is `idle_unload_s` at a few seconds;
  the app does not offer it, since every request would pay the load.
- **Apply `idle_unload_s` without a restart.** The service reads its
  settings once at start, like every other setting; a restart reloads the
  model in seconds.

## Consequences

- A service with `idle_unload_s` set answers its first request after the
  timeout about 0.25 to 0.4 s later than a warm one; with the model's files
  evicted from the page cache, somewhat more.
- An engine plugin must implement `unload()` for the setting to work with
  it; until then nothing changes for it except one log line.
- A service that is off still has a loaded LaunchDaemon and a process.
  Monitoring that counts processes sees it; `voiced status` says `off`.
- A stop (SIGTERM) that arrives while a model loads again waits for that
  load, which cannot be interrupted: under a second for the shipped
  engines with the files in the page cache, well inside launchd's 20 s
  exit timeout, but longer for a large model read from disk. The first
  load at start exits at once instead, as before; the reload does not.
- The watchdog reads one small file per service per poll.
- `voiced install` still downloads the models of a service that is off.
  Changed 2026-10-23 (ADR 0018, "Amendment (2026-10-23)"): it no longer
  does, and it does not check that service's ports either.
