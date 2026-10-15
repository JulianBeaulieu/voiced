# Operations

Running voiced on a Mac that nobody logs in to. "Service" below means the
stt or tts process; "daemon" means the launchd job that keeps it running.

## Commands

| Command | What it does |
|---|---|
| `voiced install` | Creates the service environment at `~/.voiced` with the `stt` and `tts` extras, copies `config.example.toml` to `~/.config/voiced/config.toml` if there is no config yet, downloads the configured models, writes the plists and scripts to `~/.config/voiced/launchd/`, and prints the one root step. Flags: `--dry-run` (show everything, write nothing), `--source` (a path or pip requirement to install instead of the release), `--venv`, `--python`, `--staging-dir`, `--skip-models`, `--no-uv`. |
| `voiced start [stt\|tts\|watchdog]` | Starts a loaded daemon with `launchctl kickstart`, which needs no root. A daemon that is not loaded is bootstrapped; that needs root, so the command is printed for you to run with `sudo`. |
| `voiced stop [stt\|tts\|watchdog]` | Prints `sudo launchctl bootout` for each loaded daemon. Unloading needs root, and `KeepAlive` would restart a merely killed process. Run as root, it runs them. |
| `voiced restart [stt\|tts\|watchdog\|all]` | Restarts a service now, without root: through the watchdog when its control socket answers, otherwise by sending the service `SIGKILL` and running `launchctl kickstart` directly. `all` (the default) then restarts the watchdog too, last: it sends the watchdog `SIGTERM` (the watchdog finishes the restarts it accepted before it exits), runs `launchctl kickstart`, and waits up to 30 s for the new watchdog to answer on the control socket. `watchdog` restarts only the watchdog. The exit status is non-zero if any of these restarts failed. |
| `voiced status [--json]` | Each daemon's launchd state and each service's `/health`; with the watchdog running, also each service's state, uptime and restart count from the control socket. |
| `voiced settings [get] [--json]`, `voiced settings set KEY=VALUE ...` | Shows or changes the common settings (see [Control channel](#control-channel)). `api_key=-` reads the key from stdin. |
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

`voiced doctor` checks Apple Silicon, the macOS version, Python, the
espeak-ng library Kokoro would load (the Homebrew keg or
`VOICED_ESPEAK_LIBRARY`), ffmpeg, memory, the four ports (and which process
holds them), FileVault, whether the daemons are installed and loaded, Metal
from the service environment, and the last `--metal-probe` result. It exits
non-zero only on a failed check, not on a warning.

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
| `EnvironmentVariables` | `HOME`, `PATH` (service environment first), `VOICED_CONFIG`, `PYTHONUNBUFFERED`, `VOICED_STDIO_LOG`, and `HF_HOME` when configured | The daemon reads the same config file and model cache as the CLI. `VOICED_STDIO_LOG` (`~/Library/Logs/voiced/<service>`) makes it open `<service>.stdout.log` and `.stderr.log` itself and point its stdout and stderr at them. |

The plists do not set `StandardOutPath` or `StandardErrorPath`: launchd
may open those files as root, in a log directory the user owns, so a
symlink planted there could make root create or append to any file. The
process opens them as the user instead and refuses a symlink (ADR 0006).
Plists from an older release still carry the two keys and keep working;
re-run `voiced install` and its sudo script once to replace them.

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
| `ok` | Reset the failure count; end the startup grace period. |
| Connection refused or timed out within `startup_grace_s` of the watchdog starting, of its last restart of the service, or of launchd giving the service a new pid, and before that process has answered with its model loaded | Nothing. The service is starting. |
| `starting` while the service's own uptime is below `startup_grace_s` | Nothing. The model is loading. |
| `starting` beyond `startup_grace_s` | Restart: the load is hung. |
| `degraded` | Start a timer; restart once it exceeds `degraded_window_s`. |
| Queue not empty for `stall_after_s` with no inference completing | One failure: stalled. |
| Optional probe request times out after `probe_timeout_s` | One failure: stalled. |
| Connection refused or timed out after the grace period (or after the process has answered with its model loaded), or a non-health answer | One failure. |
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

## Control channel

The watchdog also serves a Unix socket, `~/.voiced/control.sock` (in the
`[install] venv_dir` directory, so it moves with the venv), for local
clients such as a menu bar app, and for the CLI. It speaks newline-delimited
JSON, accepts only processes of the same user (mode 0600 in a 0700
directory, and the kernel's peer uid is checked), and listens on no network
interface whatever `bind` says. The design is in
[ADR 0013](decisions/0013-local-control-channel.md).

Once voiced is installed, the watchdog daemon already serves the socket:
`voiced settings`, `voiced restart` and `voiced status` use it. Do not also
run `voiced watchdog` in a terminal then; a second watchdog cannot take the
socket and would restart the services alongside the first. With nothing
answering on the socket (voiced not installed, or the watchdog stopped),
`voiced settings` changes the config file directly by the same code,
`voiced restart` restarts through `launchctl` itself, and `voiced status`
shows launchd and `/health` only. If the watchdog runs but its socket did not
start (for example a `venv_dir` path too long for a Unix socket), `voiced
status` and `voiced doctor` say so; the reason is in `voiced logs watchdog`.

```sh
voiced settings get
voiced settings set tts.voice=bf_emma tts.speed=1.1
voiced restart tts
printf '{"id":1,"op":"status"}\n{"id":2,"op":"engines.list"}\n' | nc -U ~/.voiced/control.sock
```

To try the socket without installing, run `voiced watchdog` in one terminal
and the commands above in another.

The first line from the server is `{"event": "hello", "data": {"protocol":
1, ...}}`; `status` events follow whenever something changes and at least
every 10 s, and a `settings` event follows any change to the config file.

| Operation | Arguments | Result |
|---|---|---|
| `status` | | `installed`, and per service (`stt`, `tts`, `watchdog`): `state` (`running`, `starting`, `stopped`, `degraded`, `unknown`), `pid`, `health` (the `/health` body), `engine`, `model`, `warm`, `uptime_s`, `restarts` |
| `settings.get` | | `bind` (`loopback` or `lan`), `api_key_set`, `zeroconf`, `stt`, `tts`, `browser_settings`, `ports`, `paths`. Never the API key. |
| `settings.set` | any of `bind`, `api_key`, `zeroconf`, `browser_settings`, `stt.engine`, `stt.model`, `tts.engine`, `tts.model`, `tts.voice`, `tts.speed` | the new settings plus `changed`, `restart_required` and `warnings` |
| `engines.list` | | installed engines with their `label`, models, languages, voices and `voice_labels`, without loading any |
| `service.restart` | `service`: `stt`, `tts` or `all` | `accepted` at once and `detail` per service; progress follows as `status` events |
| `logs.tail` | `service`: `stt`, `tts` or `watchdog`; `lines` up to 1000 | `lines`, each cut at 4 KiB (` [line truncated]`), from at most the last 4 MiB of the file |
| `open` | `what`: `status_page`, `logs_dir` or `config_file` | `target`, a URL or path for the client to open |

`settings.set` writes the config file atomically and changes only the
values it sets, so comments and layout stay. It never restarts anything:
`restart_required` says which services need `voiced restart` for the change
to apply (`tts.*` the tts service, `stt.*` the stt service, `bind`,
`api_key` and `zeroconf` both; `browser_settings` neither). Changing
`bind` to `lan` also starts the zeroconf announcement of the Wyoming
listeners after that restart, if `zeroconf` is on
([ADR 0011](decisions/0011-wyoming.md)). Switching `stt.engine` or
`tts.engine` without giving a model selects the new engine's own model (and
voice, if the old one does not exist there). Any registered engine can be
selected; `voiced install` installs the extras they need. A file holding an
API key is made readable by your user only. Invalid values are refused with
`invalid_setting` and the field name, and the file is left as it was.
Errors are `not_installed`, `invalid_setting`, `unknown_op`, `bad_request`,
`service_unavailable` and `internal`.

`[server] browser_settings = true` adds the same settings as a form on each
status page. It always needs the API key, so it is refused until a key is
set, on loopback too: any web page you open could otherwise reach it.

## Logs

| File | Contents |
|---|---|
| `~/Library/Logs/voiced/stt.log`, `tts.log` | Service log, rotated at 10 MB, five backups kept. |
| `~/Library/Logs/voiced/watchdog.log` | Every restart and every non-healthy observation; healthy polls only at `debug`. |
| `~/Library/Logs/voiced/<service>.stderr.log` | The daemon's stderr, opened by the daemon itself. Log records are not written here under launchd, only output that bypasses logging, such as the traceback of a crash during startup; `voiced logs --stderr` includes it. Not rotated, but stays small. |
| `~/Library/Logs/voiced/<service>.stdout.log` | The daemon's stdout; normally empty. Not rotated. |

`log_level` under `[server]` takes `debug`, `info`, `warning`, `error`.
`voiced logs -f` follows all service logs; `tail -f
~/Library/Logs/voiced/*.log` shows everything at once.

One line is expected and harmless: uvicorn logs
`ASGI callable returned without completing response` at ERROR level for each
speech client that stopped reading and was cut off after
`server.write_timeout_s`. That is a client going away, not a server error;
health is untouched. Do not alert on it. Alert on `/health` reporting
`degraded` or on the `failure` counts on the status page instead.

If the disk holding the logs fills up, the services keep serving; each log
record that cannot be written is reported on stderr instead (a `--- Logging
error ---` block), which under launchd goes to the stderr file on the same
full disk and is lost. Free space and the log resumes.

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

Plan for the two services together to take about 2.2 GB idle, more than
3 GB at moments while serving (Kokoro's long replies and Parakeet's long input each
add 1.2 to 1.5 GB; about 5 GB if both peak at once), and about 4.5 GB while the
stt service starts
([ADR 0015](decisions/0015-integration-results.md#known-misses)). On a Mac that also
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

### Homebrew

With the formula ([ADR 0017](decisions/0017-homebrew-packaging.md)),
`~/.voiced/bin` holds two launchers that run voiced from
`$(brew --prefix)/opt/voiced/libexec`, which Homebrew repoints at the new
version on every upgrade. The plists do not change, so there is no root
step:

```sh
brew upgrade voiced              # brew upgrade --fetch-HEAD voiced for a --HEAD install
voiced restart all
voiced status
```

Until they restart, the running daemons keep the old version's files open;
Homebrew deletes the old version afterwards, and a daemon that exits for
any reason comes back on the new one. `voiced restart all` makes that
immediate. If the changelog says a release changes the plists, run
`voiced install` and the printed sudo step as well.

To remove voiced, run `voiced uninstall`, its sudo step and
`voiced uninstall --yes` first, then `brew uninstall voiced`. Uninstalling
the formula first leaves launchd restarting a launcher with nothing behind
it every 10 s.

To switch an existing source install to Homebrew: stop the daemons
(`voiced stop all` and the `sudo` line it prints), `rm -rf ~/.voiced` (the
models, config and logs live elsewhere), then `voiced install` from the
Homebrew `voiced`; the plists are the same, so the sudo step that follows is
the usual one.

### From a checkout

Re-running `voiced install` upgrades in place: it reuses the service
environment, upgrades the package, re-stages the plists and prints the
install script, which unloads and reloads the daemons. Models and the
config file are kept.

From the checkout in `~/voiced`, before the first release:

```sh
cd ~/voiced
git pull
uv tool install --reinstall --python 3.12 ~/voiced
voiced install --source ~/voiced
sudo bash ~/.config/voiced/launchd/install-daemons.sh
```

Use `--reinstall`: `--force` does not rebuild a version uv already has, so
the command would keep the old code while the service environment updates.

The sudo script is only needed when the plists change (a new label, log
directory, service environment or user); `diff` the staged ones in
`~/.config/voiced/launchd/` against `/Library/LaunchDaemons/` to check. A
code-only upgrade needs no root:

```sh
uv tool install --reinstall --python 3.12 ~/voiced
voiced install --source ~/voiced
voiced restart all
```

`voiced restart all` restarts stt and tts, then the watchdog, and reports
once the new watchdog answers on the control socket. `voiced restart stt`
or `tts` leaves the watchdog running its old code; `voiced restart
watchdog` restarts it alone.

Read [CHANGELOG.md](../CHANGELOG.md) for anything that needs a manual step.
