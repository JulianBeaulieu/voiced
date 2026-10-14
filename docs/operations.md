# Operations

Running voiced on a Mac that nobody logs in to. "Service" below means the
stt or tts process; "daemon" means the launchd job that keeps it running.

## Commands

| Command | What it does |
|---|---|
| `voiced install` | Creates the service environment at `~/.voiced` with the `stt` and `tts` extras, copies `config.example.toml` to `~/.config/voiced/config.toml` if there is no config yet, downloads the configured models, writes the plists and scripts to `~/.config/voiced/launchd/`, and prints the one root step. Flags: `--dry-run` (show everything, write nothing), `--source` (a path or pip requirement to install instead of the release), `--venv`, `--python`, `--user`, `--staging-dir`, `--skip-models`, `--no-uv`. |
| `voiced start [stt\|tts\|watchdog]` | Starts a loaded daemon with `launchctl kickstart`, which needs no root. A daemon that is not loaded is bootstrapped; that needs root, so the command is printed for you to run with `sudo`. |
| `voiced stop [stt\|tts\|watchdog]` | Prints `sudo launchctl bootout` for each loaded daemon. Unloading needs root, and `KeepAlive` would restart a merely killed process. Run as root, it runs them. |
| `voiced status [--json]` | Each daemon's launchd state and each service's `/health`. |
| `voiced logs [stt\|tts\|watchdog] [-n N] [-f] [--stderr]` | The application logs; `--stderr` adds launchd's stderr capture. |
| `voiced doctor [--json]` | Checks the machine (see below). |
| `voiced doctor --metal-probe` | Stages a one-shot LaunchDaemon that tests Metal from a real daemon context and prints the `sudo` command to run it. A later `voiced doctor` reads the result. |
| `voiced uninstall` | Prints the root script that unloads and removes the daemons, then lists what it would delete. `--yes` deletes the service environment; `--remove-config`, `--remove-logs` and `--remove-models` add those. It refuses to delete anything while a daemon is still loaded. |
| `voiced watchdog [--once]` | Runs the watchdog in the foreground; `--once` polls once and prints its decisions. |
| `voiced serve stt\|tts` | Runs one service in the foreground. For development and for trying an engine before installing. |
| `voiced config` | Prints the effective configuration as JSON. |

voiced never runs root steps itself. Anything that touches
`/Library/LaunchDaemons` is printed as a short script or command for you to
read and run with `sudo`. To apply a config change, re-run the install
script; it unloads and reloads all three daemons:

```sh
sudo bash ~/.config/voiced/launchd/install-daemons.sh
```

`voiced doctor` checks Apple Silicon, the macOS version, Python, espeak-ng,
ffmpeg, memory, the four ports (and which process holds them), FileVault,
whether the daemons are installed and loaded, Metal from the service
environment, and the last `--metal-probe` result. It exits non-zero only on
a failed check, not on a warning.

## launchd

Three LaunchDaemons, labels `<prefix>.stt`, `<prefix>.tts` and
`<prefix>.watchdog`. The default prefix is `io.github.julianbeaulieu.voiced`;
change it with `[launchd] label_prefix` before installing if you need a
different namespace.

Each plist sets:

| Key | Value | Why |
|---|---|---|
| `UserName` | the installing user | The models and config live in that user's home; nothing runs as root. |
| `RunAtLoad` | true | Start at boot. |
| `KeepAlive` | true | Restart on exit, whatever the exit status. |
| `ThrottleInterval` | 10 | Wait 10 s between restarts so a crash loop does not spin. |
| `ProcessType` | Interactive | Full CPU and GPU scheduling priority. |
| `WorkingDirectory` | the user's home | |
| `EnvironmentVariables` | `HOME`, `PATH` (service environment first), `VOICED_CONFIG`, `PYTHONUNBUFFERED`, and `HF_HOME` when configured | The daemon reads the same config file and model cache as the CLI. |
| `StandardOutPath`, `StandardErrorPath` | `~/Library/Logs/voiced/<service>.stdout.log`, `.stderr.log` | Catch output before logging is configured. |

Daemons rather than LaunchAgents because agents only start once their user
logs in to the GUI. Metal works from a daemon context on Apple Silicon: a
probe daemon on macOS 15.7 with MLX 0.32 saw the GPU and ran a matrix
multiply as a normal user with no GUI session
([ADR 0004](decisions/0004-launchd-daemon-vs-agent.md)). Run
`voiced doctor --metal-probe` to confirm it on your machine and macOS
version; `voiced doctor` only accepts a probe result recorded by launchd's
system manager on the macOS version that is running now.

Useful launchctl commands, with the default prefix:

```sh
launchctl print system/io.github.julianbeaulieu.voiced.tts           # state, pid, last exit
sudo launchctl kickstart -k system/io.github.julianbeaulieu.voiced.tts   # force restart
sudo launchctl bootout system/io.github.julianbeaulieu.voiced.tts        # unload
```

## Watchdog

`KeepAlive` only restarts a process that exits. MLX can wedge a process that
stays alive: the GPU queue never drains while the process keeps running. The
watchdog daemon catches that. Every `interval_s` it requests `/health` from
each service with a `timeout_s` limit and applies these rules:

| Observation | Action |
|---|---|
| `ok` | Reset the failure count. |
| Connection refused within `startup_grace_s` of the watchdog starting, of its last restart of the service, or of launchd giving the service a new pid | Nothing. The service is starting. |
| `starting` while the service's own uptime is below `startup_grace_s` | Nothing. The model is loading. |
| `starting` beyond `startup_grace_s` | Restart: the load is hung. |
| `degraded` | Start a timer; restart once it exceeds `degraded_window_s`. |
| Queue not empty for `stall_after_s` with no inference completing | One failure: stalled. |
| Optional probe request times out after `probe_timeout_s` | One failure: stalled. |
| Connection refused after the grace period, timeout, or a non-health answer | One failure. |
| `failures_before_restart` consecutive failures | Restart. |
| The daemon is not loaded (after `voiced stop`) | Never restart; logged once. |

| Key under `[watchdog]` | Default |
|---|---|
| `interval_s` | 15 |
| `timeout_s` | 5 |
| `failures_before_restart` | 3 |
| `startup_grace_s` | 300 |
| `degraded_window_s` | 120 |
| `stall_after_s` | 60 |
| `restart_cooldown_s` | 120 |
| `probe` | false |
| `probe_timeout_s` | 20 |

A restart reads the pid from `launchctl print`, sends it `SIGKILL` and runs
`launchctl kickstart` so the service comes back at once instead of after the
throttle interval. The watchdog runs as the installing user, which can kill
its own processes and kickstart a loaded daemon but cannot use
`kickstart -k`; running it as root would mean executing the user-writable
service environment as root. A second restart of the same service within
`restart_cooldown_s` waits for the cooldown.

The optional probe (`probe = true`) sends a tiny real request each poll
while the queue is empty, which finds a GPU that wedged while idle. It is
off by default because it costs an inference per poll; without it, an idle
wedge is found at the first real request plus `stall_after_s`.

`voiced watchdog --once` shows what the watchdog thinks of the machine right
now. The design is in [ADR 0012](decisions/0012-watchdog.md).

## Logs

| File | Contents |
|---|---|
| `~/Library/Logs/voiced/stt.log`, `tts.log` | Service log, rotated at 10 MB, five backups kept. |
| `~/Library/Logs/voiced/watchdog.log` | Every restart and every non-healthy observation; healthy polls only at `debug`. |
| `~/Library/Logs/voiced/<service>.stderr.log` | launchd's capture of stderr. The services also log every line to stderr, so this file repeats the service log, plus tracebacks from a failed startup. It is not rotated; truncate it by hand if it grows. |
| `~/Library/Logs/voiced/<service>.stdout.log` | launchd's capture of stdout; normally empty. Not rotated. |

`log_level` under `[server]` takes `debug`, `info`, `warning`, `error`.
`voiced logs -f` follows all service logs; `tail -f
~/Library/Logs/voiced/*.log` shows everything at once.

One line is expected and harmless: uvicorn logs
`ASGI callable returned without completing response` at ERROR level for each
speech client that stopped reading and was cut off after
`server.write_timeout_s`. That is a client going away, not a server error;
health is untouched. Do not alert on it. Alert on `/health` reporting
`degraded` or on the `failure` counts on the status page instead.

## FileVault

With FileVault on, a reboot stops at the disk-unlock screen and nothing
starts until someone types the password there. After the unlock, launchd
starts the daemons without a GUI login; nobody needs to sign in. `voiced
doctor` warns when FileVault is on so you know a remote reboot will wait for
a keyboard.

If the Mac must come back unattended, either turn FileVault off (the disk is
then readable by anyone with physical access) or keep it on and accept that
reboots need a person. voiced does not change this setting.

## Memory budget

| Service | Model | Memory |
|---|---|---|
| tts | Kokoro-82M (default) | 469 MB peak resident |
| tts | Soprano-80M (opt-in) | 392 MB resident |
| stt | Parakeet TDT 0.6B v3 | 1.28 GB after load, 2.46 GB serving peak, 3.6 to 4.1 GB once while loading |

All figures were measured on an M1 Max. Parakeet's are MLX allocations
(`mx.get_peak_memory()`). Batch input longer than 20 s is transcribed in
overlapping 20 s chunks and MLX's buffer cache is emptied after each request,
so the serving peak holds for any input length. The load peak is a transient:
parakeet-mlx reads the fp32 weights and then casts them to bf16
([ADR 0008](decisions/0008-stt-engine-parakeet.md)).

Plan for the two services together to stay under 3 GB while serving, and
for about 4.5 GB while the stt service starts. On a Mac that also
runs an LLM server, add up what each resident model needs on top of the few
gigabytes macOS itself uses, and leave headroom. Apple Silicon shares memory
between CPU and GPU, so there is no separate VRAM to run out of, but the GPU
can only wire about two thirds of total memory by default; models that do
not fit in that limit page and get slow.

## Other headless Mac settings

None of these are voiced features, and the installer does not touch them.
They are the usual steps for a Mac that runs as a server:

- Raise the GPU wired limit if you also run large LLMs, for example
  `sudo sysctl iogpu.wired_limit_mb=24576` on a 32 GB machine. The value
  resets at reboot; a small LaunchDaemon that runs that `sysctl` at load
  makes it stick. Leave at least 8 GB for macOS and the CPU side; `0`
  restores the default.
- Keep the Mac awake and bring it back after power loss:
  `sudo pmset -a sleep 0 disksleep 0 autorestart 1 womp 1`.
- Exclude the Hugging Face cache from Spotlight under **System Settings**,
  **Siri and Spotlight**, **Spotlight Privacy**.

## Updating

Re-running `voiced install` upgrades in place: it reuses the service
environment, upgrades the package, re-stages the plists and prints the
install script, which unloads and reloads the daemons. Models and the
config file are kept.

From a checkout, before the first release:

```sh
cd voiced
git pull
uv tool install --reinstall .
voiced install --source "$PWD"
sudo bash ~/.config/voiced/launchd/install-daemons.sh
```

Read [CHANGELOG.md](../CHANGELOG.md) for anything that needs a manual step.
