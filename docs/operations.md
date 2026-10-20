# Operations

Running voiced on a Mac that nobody logs in to. "Service" below means the
stt or tts process; "daemon" means the launchd job that keeps it running.

## Commands

The formula puts `voiced` on your `PATH`. An install made by the menu bar
app does not: run the commands as `~/.voiced/bin/voiced`, or add
`~/.voiced/bin` to your `PATH`.

| Command | What it does |
|---|---|
| `voiced install` | Links `~/.voiced/bin/voiced`, the program the plists run, to an environment that has the engines: the one this `voiced` runs from (Homebrew's, or any other with the `stt` and `tts` extras), or with `--source <checkout>` a new one it builds into `~/.voiced/envs/` from the shipped lock. Copies `config.example.toml` to `~/.config/voiced/config.toml` if there is no config yet, checks that the four service ports are free (bound as the services bind them, and with `lsof`; a port another program holds stops the install, naming the port, its config key and the holder, while voiced's own running services are fine), downloads the configured models, removes the plists and sudo scripts an earlier release staged in `~/.config/voiced/launchd/`, and prints the one root step (below). Refuses a `~/.voiced` holding files that are not voiced's. Flags: `--dry-run` (show everything, write nothing), `--source`, `--python` (the Python version for `--source`), `--state-dir`, `--skip-models`, `--origin` (`cli`, `homebrew` or `app`, for `install.json`). Waits while another install or uninstall holds the state directory; changes nothing the daemons use until the models are in. The layout is in [ADR 0017](decisions/0017-homebrew-packaging.md#the-install-layout). |
| `voiced start [stt\|tts\|watchdog]` | Starts a loaded daemon with `launchctl kickstart`, which needs no root. A daemon that is not loaded is bootstrapped; that needs root, so the command is printed for you to run with `sudo`. |
| `voiced stop [stt\|tts\|watchdog]` | Prints `sudo launchctl bootout` for each loaded daemon. Unloading needs root, and `KeepAlive` would restart a merely killed process. Run as root, it runs them. |
| `voiced restart [stt\|tts\|watchdog\|all]` | Restarts a service now, without root: through the watchdog when its control socket answers, otherwise by sending the service `SIGKILL` and running `launchctl kickstart` directly. `all` (the default) then restarts the watchdog too, last: it sends the watchdog `SIGTERM` (the watchdog finishes the restarts it accepted before it exits), runs `launchctl kickstart`, and waits up to 30 s for the new watchdog to answer on the control socket. `watchdog` restarts only the watchdog. The exit status is non-zero if any of these restarts failed. |
| `voiced status [--json]` | Each daemon's launchd state and each service's `/health`; with the watchdog running, also each service's state, uptime and restart count from the control socket. |
| `voiced settings [get] [--json]`, `voiced settings set KEY=VALUE ...` | Shows or changes the common settings (see [Control channel](#control-channel)). `api_key=-` reads the key from stdin. |
| `voiced logs [stt\|tts\|watchdog] [-n N] [-f] [--stderr]` | The application logs; `--stderr` adds launchd's stderr capture. |
| `voiced doctor [--json]` | Checks the machine (see below). |
| `voiced doctor --metal-probe` | Writes `~/.config/voiced/metal-probe.sh`, a script that installs a one-shot LaunchDaemon testing Metal from a real daemon context and removes it again, with its plist inside, and prints the `sudo` command to run it. A later `voiced doctor` reads the result. |
| `voiced uninstall` | Prints the root step that unloads and removes the daemons, then lists what it would delete. `--yes` deletes what `voiced install` put in `~/.voiced` (the link, the environments it built, their Pythons, `install.json`) and keeps anything else there, and never an environment elsewhere such as Homebrew's; `--remove-config`, `--remove-logs` and `--remove-models` add those. `--remove-models` deletes the model revisions voiced pinned from the Hugging Face cache, through `huggingface_hub`, and says how many bytes that freed; it keeps other revisions and repositories in the cache, which other tools may use, and a configured model voiced does not pin. When another tool downloaded a model's `main` and `main` is the pinned commit, that is the same revision: the dry run says so before `--yes` deletes it. It refuses to delete anything while a daemon is still loaded. |
| `voiced watchdog [--once]` | Runs the watchdog in the foreground; `--once` polls once and prints its decisions. |
| `voiced serve stt\|tts` | Runs one service in the foreground. For development and for trying an engine before installing. |
| `voiced config` | Prints the effective configuration as JSON. |

voiced never runs root steps itself. Anything that touches
`/Library/LaunchDaemons` is printed as a command for you to run with `sudo`.
The daemons are installed and removed by one script, the one the menu bar
app runs too (ADR 0018), shipped in the voiced package:

```sh
sudo /bin/bash <environment>/lib/python3.12/site-packages/voiced/launchd/voiced-privileged.sh install <you>
sudo /bin/bash <same path> uninstall <you>
```

`voiced install` and `voiced uninstall` print the exact lines. Its only
input is your user name: it renders the three plists itself from fixed
values and your home directory, refuses system accounts and another
account's install (one install per Mac), and allows one run at a time. It
exits 0 when done, 1 when it refused (nothing written), 3 when launchd did
not load a label, 4 when the firewall exception could not be added and 5
when the firewall is on but the environment's Python is outside your home
(Homebrew's) and was left alone; 4 and 5 come after a successful install.
The file is in the environment voiced runs from, which your account can
write: the same trust as any script you run with `sudo`. `voiced install`
prints the line, with the path fully resolved, only if the file and every
directory on the way to it belong to you or root and no other account can
write them, by mode bits or by an ACL; a Homebrew owned by another admin,
or a group-writable directory with other members, gets no root step and a
pointer to `voiced install --source`. An environment on a volume mounted
with ownership ignored cannot be checked this way: every file there looks
like yours. Read it first if you like. Re-running it is safe.

The plists name only `~/.voiced/bin/voiced`, `~/.config/voiced/config.toml`
and the default labels, so the services take everything else from the
config file when they start: a config change needs `voiced restart all`,
not root. An install with another state directory, config path or
`[launchd] label_prefix` gets no root step: `voiced install` and `voiced
uninstall` say so, and such a layout runs with `voiced serve` instead.

`voiced doctor` checks Apple Silicon, the macOS version, Python, the
espeak-ng library Kokoro would load (`[tts] espeak_library`, else the
`espeakng-loader` wheel's in voiced's environment, else the Homebrew keg),
ffmpeg, memory, the four ports (and which process holds them), FileVault,
whether the daemons are installed and loaded, Metal from the service
environment, and the last `--metal-probe` result. It exits non-zero only on
a failed check, not on a warning.

## Installing from a checkout

For development, or to run `main`. You need
[uv](https://docs.astral.sh/uv/). The `tts` extra brings its own espeak-ng;
Homebrew's is optional, as the fallback or for the `[tts] espeak_library`
override:

```sh
brew install uv            # skip if `uv --version` already works
brew install espeak-ng     # optional
```

Then:

```sh
git clone https://github.com/JulianBeaulieu/voiced ~/voiced
uv tool install --python 3.12 ~/voiced
uv tool update-shell
```

`--python 3.12` matters: without it uv uses its default interpreter, which
can be one voiced does not support (it needs 3.11 to 3.13). With 3.10 the
install fails with "requirements are unsatisfiable"; with 3.14 it can
succeed on a Python voiced is not tested on, which `voiced doctor` then
reports as a failure. `uv tool install` puts the `voiced` command in
`~/.local/bin`, which is often not on your `PATH` yet; `uv tool
update-shell` adds it to your shell's startup file (`~/.zshenv` for the
default zsh). Open a new terminal, then:

```sh
voiced install --source ~/voiced
```

`uv tool install` gives you the `voiced` command but not the engines;
`--source` makes `voiced install` build the service environment, with the
engines, from your checkout into `~/.voiced/envs/`, every package at the
version and hash in the checkout's lock, on a Python 3.12 that uv downloads
into `~/.voiced/python`. It then points `~/.voiced/bin/voiced` at that
environment, downloads the models into the Hugging Face cache, and prints
the root step above. A warning about `HF_TOKEN` along the way is harmless:
the models are public and need no Hugging Face account.
`voiced install --source ~/voiced --dry-run` shows everything it would do
without writing anything.

To build the menu bar app from the same checkout, run
`~/voiced/menubar/Scripts/bundle.sh` and open
`~/voiced/menubar/build/voiced.app`. It needs Swift 6 (Xcode 16 or newer);
see [menubar/README.md](../menubar/README.md).

## launchd

Three LaunchDaemons, labels `io.github.julianbeaulieu.voiced.stt`, `.tts`
and `.watchdog`. `[launchd] label_prefix` changes only which labels
`voiced status`, `start`, `stop`, `restart`, `doctor` and the watchdog
address, for daemons installed some other way, such as by an earlier
release; the root step installs the default labels. Daemons an earlier
release installed under another prefix are removed by hand:
`sudo /bin/launchctl bootout system/<label>`, then
`sudo /bin/rm /Library/LaunchDaemons/<label>.plist`, for each of the three.

Each plist sets:

| Key | Value | Why |
|---|---|---|
| `UserName` | the installing user | The models and config live in that user's home; nothing runs as root. |
| `RunAtLoad` | true | Start at boot. |
| `KeepAlive` | true | Restart on exit, whatever the exit status. |
| `ThrottleInterval` | 10 | Wait 10 s between restarts so a crash loop does not spin. |
| `ProcessType` | Interactive | Full CPU and GPU scheduling priority. |
| `WorkingDirectory` | the user's home | |
| `EnvironmentVariables` | `HOME`, `PATH` (service environment first), `VOICED_CONFIG`, `PYTHONUNBUFFERED`, `VOICED_STDIO_LOG`, nothing else | The daemon reads the same config file as the CLI, and takes the model cache (`[install] hf_home`), the espeak-ng paths (`[tts] espeak_library`, `espeak_data`) and a custom `log_dir` from it, so the plists are the same fixed values the menu bar app renders (ADR 0018). `VOICED_STDIO_LOG` (`~/Library/Logs/voiced/<service>`) makes it open `<service>.stdout.log` and `.stderr.log` itself and point its stdout and stderr at them; with a custom `log_dir` it moves them there once the config is read. |

The plists do not set `StandardOutPath` or `StandardErrorPath`: launchd
may open those files as root, in a log directory the user owns, so a
symlink planted there could make root create or append to any file. The
process opens them as the user instead and refuses a symlink (ADR 0006).
Plists from an older release still carry the two keys and keep working;
run the root step once to replace them.

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
`[install] venv_dir` state directory, so it moves with it), for local
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
| tts | Phonon 31M (opt-in, on the CPU) | 0.26 GB footprint, 0.53 GB resident after a long reply |
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

The plists run `~/.voiced/bin/voiced`, a link to the active environment,
and are the same for every way of installing, so an update that only
changes the environment needs no root step. The watchdog restarts both
services onto the new environment and then itself, within one poll
(`[watchdog] interval_s`) of either of two things: a file it runs from
was deleted or replaced (the old Homebrew keg or Python removed, a keg
rebuilt by `brew reinstall`, an old `envs/<id>` pruned), or
`~/.voiced/bin/voiced` was repointed (`voiced install`, the app). While
Homebrew is installing voiced or its Python, the watchdog neither polls
nor restarts anything, and refuses `voiced restart` (it exits non-zero
with the reason), for up to 30 minutes.
`voiced restart all` does it at once. If the changelog says a release
changes the plists, run the root step `voiced install` prints as well.

### The menu bar app

The app does not update itself. Get the new version with
`brew upgrade --cask voiced`, or download it from the
[release page](https://github.com/JulianBeaulieu/voiced/releases/latest)
and replace the copy in Applications. On its next launch the app sees that
the services run an older voiced and offers **Update voiced**.

### Homebrew

```sh
brew upgrade voiced
voiced restart all
voiced status
```

A plain `brew upgrade voiced` does nothing for a `--HEAD` install. The link
goes through `$(brew --prefix)/opt/voiced`, which Homebrew repoints at the
new version, so `voiced install` is not needed again. The same applies to
an upgrade of `python@3.12`. The watchdog moves the services once
Homebrew deletes the old version, at the end of `brew upgrade`, and after
`brew reinstall voiced` once it has returned. With
`HOMEBREW_NO_INSTALL_CLEANUP` set, `brew upgrade` deletes nothing and the
services stay on the old version (which keeps working) until the next
`brew cleanup` or `voiced restart all`. Run `voiced restart all` only
after `brew upgrade` has returned: Homebrew repoints `opt/voiced` before
it has finished the new version, and a voiced started in between can
leave some of its files unloadable (macOS kills, or hangs, every later
process that loads them). `brew reinstall voiced` writes them anew.

To remove voiced, run `voiced uninstall`, its sudo step and
`voiced uninstall --yes` first, then `brew uninstall voiced`. Homebrew does
not show the caveats at uninstall. Uninstalling the formula first leaves
the daemons loaded with `~/.voiced/bin/voiced` pointing nowhere; launchd
then stops restarting them (their `KeepAlive` holds only while that path
exists) and `voiced doctor` reports the dangling link. Reinstall the formula
to bring them back, or run the uninstall steps above. If a daemon stays
loaded but not running after the link is back (`voiced doctor`: "loaded
but not running"; launchd can leave such a job at `spawn scheduled`),
re-run the root step `voiced install` prints: it boots the daemons out
and in again, which starts them.

### From a checkout

```sh
cd ~/voiced
git pull
uv tool install --reinstall --python 3.12 ~/voiced
voiced install --source ~/voiced
voiced restart all
```

Each `voiced install --source` builds a fresh environment in
`~/.voiced/envs/` from `src/voiced/requirements.lock` (every package with
its hash, then voiced itself), checks that it imports the engines, and
repoints the link; the daemons keep running the previous environment until
they restart. It keeps the new environment and the previous one and removes
older ones. Models and the config file are kept. Every run builds a new
environment, even from an unchanged checkout, and the watchdog then
restarts the services onto it; to change only the config or the plists,
run `voiced install` from the active environment
(`~/.voiced/bin/voiced install`), which builds nothing and keeps the
environments. Use `--reinstall`:
`--force` does not rebuild a version uv already has, so the `voiced` command
would keep the old code.

### Switching between Homebrew and a checkout

Run `voiced install` from the one you are switching to, then
`voiced restart all`. No root step, nothing to delete by hand:

```sh
# to Homebrew
brew install julianbeaulieu/voiced/voiced
$(brew --prefix)/bin/voiced install
voiced restart all

# to a checkout
voiced install --source ~/voiced
voiced restart all
```

A `~/.voiced` from before ADR 0017 is a venv itself. The first switch away
from it removes that venv's files (`lib`, `include`, `pyvenv.cfg`, the rest
of `bin`) once the new environment is verified and the models are in;
there is no going back to it, only forward to a new
`voiced install --source`. Run `voiced restart all` straight after: the
running daemons still use the removed venv's files.

`voiced restart all` restarts stt and tts, then the watchdog, and reports
once the new watchdog answers on the control socket. `voiced restart stt`
or `tts` leaves the watchdog running its old code; `voiced restart
watchdog` restarts it alone.

Read [CHANGELOG.md](../CHANGELOG.md) for anything that needs a manual step.
