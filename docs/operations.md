# Operations

Running voiced on a Mac, including one that serves Home Assistant
unattended. "Service" below means the stt or tts process; "agent" means
the launchd job that keeps it running. Nothing here needs an administrator
password, except once to remove the LaunchDaemons of 0.4.0 or earlier
([Moving from LaunchDaemons](#moving-from-launchdaemons)).

## Commands

The formula puts `voiced` on your `PATH`. An install made by the menu bar
app does not: run the commands as `~/.voiced/bin/voiced`, or add
`~/.voiced/bin` to your `PATH`.

| Command | What it does |
|---|---|
| `voiced install` | Links `~/.voiced/bin/voiced`, the program the plists run, to an environment that has the engines: the one this `voiced` runs from (Homebrew's, or any other with the `stt` and `tts` extras), or with `--source <checkout>` a new one it builds into `~/.voiced/envs/` from the shipped lock. Copies `config.example.toml` to `~/.config/voiced/config.toml` if there is no config yet, checks that the ports of each service that is on are free (bound as the services bind them, and with `lsof`; a port another program holds stops the install, naming the port, its config key and the holder, while voiced's own running services are fine), downloads the configured models of each service that is on, removes the plists and sudo scripts an earlier release staged in `~/.config/voiced/launchd/`, and last writes the three LaunchAgents to `~/Library/LaunchAgents` and loads them into your launchd domain (`gui/<uid>`); an agent whose plist is unchanged and loaded keeps running. Stops before it changes anything when you are not logged in at the Mac (over SSH with nobody at the console there is nowhere to load the agents; Screen Sharing counts as logged in), and while LaunchDaemons from 0.4.0 or earlier are installed, printing the one line that removes them ([below](#moving-from-launchdaemons)); `--dry-run` refuses the same way. Refuses a `~/.voiced` holding files that are not voiced's. Flags: `--dry-run` (show everything, write nothing), `--source`, `--python` (the Python version for `--source`), `--state-dir`, `--skip-models`, `--origin` (`cli`, `homebrew` or `app`, for `install.json`), `--set KEY=VALUE` (repeatable, `stt.enabled`, `stt.engine`, `stt.model` and the same for `tts` only, an engine with its model: the install plans, checks ports and downloads models with these settings, and writes them to the config once the models are in, with every check of `voiced settings set`; a failed download leaves the config as it was; the app's first-run setup passes its choice this way, such as `--set tts.enabled=false`). Waits while another install or uninstall holds the state directory; changes nothing the services use until the models are in. The layout is in [ADR 0017](decisions/0017-homebrew-packaging.md#the-install-layout). |
| `voiced start [stt\|tts\|watchdog]` | Starts a loaded agent with `launchctl kickstart`; an installed agent that is not loaded (after `voiced stop`) is loaded with `launchctl bootstrap`. |
| `voiced stop [stt\|tts\|watchdog]` | Unloads each loaded agent with `launchctl bootout` (`KeepAlive` would restart a merely killed process). It stays stopped until `voiced start` or your next login. |
| `voiced restart [stt\|tts\|watchdog\|all]` | Restarts a service now: through the watchdog when its control socket answers, otherwise by sending the service `SIGKILL` and running `launchctl kickstart` directly. `all` (the default) then restarts the watchdog too, last: it sends the watchdog `SIGTERM` (the watchdog finishes the restarts it accepted before it exits), runs `launchctl kickstart`, and waits up to 30 s for the new watchdog to answer on the control socket. `watchdog` restarts only the watchdog. The exit status is non-zero if any of these restarts failed. |
| `voiced status [--json]` | Each agent's launchd state and each service's `/health`; with the watchdog running, also each service's state, uptime and restart count from the control socket. |
| `voiced settings [get] [--json]`, `voiced settings set KEY=VALUE ...` | Shows or changes the common settings (see [Control channel](#control-channel)). `api_key=-` reads the key from stdin. |
| `voiced logs [stt\|tts\|watchdog] [-n N] [-f] [--stderr]` | The application logs; `--stderr` adds launchd's stderr capture. |
| `voiced doctor [--json]` | Checks the machine (see below). |
| `voiced doctor --metal-probe` | Runs a one-shot LaunchAgent that tests Metal as the services run, from a plist in a temporary directory, waits up to 60 s for its result, unloads it and prints the result. A later `voiced doctor` reads it. |
| `voiced uninstall` | Lists what it would remove, the LaunchAgents first. `--yes` unloads the agents and deletes their plists, then deletes what `voiced install` put in `~/.voiced` (the link, the environments it built, their Pythons, `install.json`) and keeps anything else there, and never an environment elsewhere such as Homebrew's; `--remove-config`, `--remove-logs` and `--remove-models` add those. `--remove-models` deletes the model revisions voiced pinned from the Hugging Face cache, through `huggingface_hub`, and says how many bytes that freed; it keeps other revisions and repositories in the cache, which other tools may use, and a configured model voiced does not pin. When another tool downloaded a model's `main` and `main` is the pinned commit, that is the same revision: the dry run says so before `--yes` deletes it. While LaunchDaemons from 0.4.0 or earlier are installed it prints the line that removes them and deletes nothing. |
| `voiced models list [--json]` | Every model voiced can download, by engine: licence, download size or size on disk, and state (in use, downloaded, not downloaded, update available, an old version cached). See [Models](#models). |
| `voiced models download <model> [--json-progress]` | Downloads a model at its pinned revision, with its engine's shared files (Phonon's voices). A repository voiced does not know is fetched whole at `main`, unpinned, with a notice. |
| `voiced models delete <model> [--previous] [--yes] [--json]` | Deletes a model's pinned revision (`--previous`: only its older pinned revisions) from the Hugging Face cache. Refuses the model the config uses and anything voiced does not pin; asks first without `--yes`. |
| `voiced watchdog [--once]` | Runs the watchdog in the foreground; `--once` polls once and prints its decisions. |
| `voiced serve stt\|tts` | Runs one service in the foreground. For development and for trying an engine before installing. |
| `voiced config` | Prints the effective configuration as JSON. |

The agents' plists follow the install's layout: `~/Library/LaunchAgents/<label>.plist`
runs `<state dir>/bin/voiced` with `VOICED_CONFIG` set to the config file the
install used, under the labels `[launchd] label_prefix` gives. The services
take everything else from that config file when they start, so a config
change needs `voiced restart all`, not a new install. A scratch layout (a
`--state-dir`, a `VOICED_CONFIG` and another `label_prefix`) installs agents
of its own beside the real ones, which is how a harness tests a change
without touching your install.

`voiced doctor` checks Apple Silicon, the macOS version, Python, the
espeak-ng library Kokoro would load (`[tts] espeak_library`, else the
`espeakng-loader` wheel's in voiced's environment, else the Homebrew keg),
ffmpeg, memory, the four ports (and which process holds them), whether
you are logged in at the Mac (what `voiced install` needs), whether
voiced starts after a reboot (FileVault or automatic login), LaunchDaemons
left from 0.4.0 or earlier, each service's configured model (downloaded at
its pin, or the command that fetches it), whether the agents are installed
and loaded, the control socket, Metal from the service environment, the
last `--metal-probe` result and, with `bind` on the network, whether the
firewall is on (macOS then asks at the Mac whether the services' Python
may accept connections, again after an update that changes it) and
whether the services' Bonjour announcements reach the network (`dns-sd`
sees them on an interface other than loopback; macOS's Local Network
privacy can keep them on this Mac, ADR 0035). It exits non-zero only on
a failed check, not on a warning.

## Models

voiced knows its models from a catalogue in `voiced/models/catalogue.py` (each
engine's models, by the id the config uses, with the Hugging Face
repository, licence, whether it is gated, and the files the engine loads)
and pins each one in `voiced/models/models.lock.json` (the repository's commit,
every file with its size and sha256, and the revisions earlier releases
pinned).
They live in the Hugging Face cache, `[install] hf_home` (default
`~/.cache/huggingface/hub`; voiced sets `HF_HOME` itself, so
`XDG_CACHE_HOME` does not move it), which other MLX tools share; with
`huggingface_hub` 2.x the contents of files stored with Xet live once in a
store for the whole cache (`hub/blobs/`), and each repository's folder
links into it; other files sit in the repository's own folder. A model
downloaded while its service runs is loaded by `voiced restart <service>`.

- `voiced install` downloads the configured models; `voiced models
  download` any other. The services never download: they run with
  `HF_HUB_OFFLINE=1`, and a model that is missing at start makes `/health`
  say `missing_model` (503) with the command in `last_error`; the process
  stays up and the watchdog does not restart it.
- `voiced settings set` (and the menu bar app, and the status page form)
  refuses to switch to a catalogued model that is not complete in the
  cache, with the error code `model_not_downloaded`.
- `voiced models delete` removes only a `(repository, revision)` pair the
  lock names, through `huggingface_hub`: the snapshot, its refs (a `main`
  another tool fetched at the same commit goes too, and the command says
  so), the blobs no other revision uses, and the repository folder when
  nothing else of it is left. The last model of an engine takes the engine's
  shared repository with it. Models that are folders of one repository share
  its revision, which is deleted whole, so deleting one while another of
  them is downloaded is refused. Revisions and repositories voiced does not pin
  are never touched. `voiced uninstall --remove-models` deletes the same
  way, every revision the lock names.
- `download` and `delete` take the install lock without waiting: while an
  install, update or uninstall runs they fail with `busy`.

`voiced models list --json`, which the menu bar app reads:

```json
{
  "cache": "/Users/you/.cache/huggingface/hub",
  "total_bytes": 2864000000,
  "engines": [
    {
      "name": "phonon", "label": "Phonon", "kind": "tts", "installed": true,
      "unavailable_reason": null,
      "languages": ["en-US", "en-GB"], "voices": ["default", "Zoey"],
      "models": [
        {
          "id": "gradium/phonon-31m-en", "repo": "gradium/phonon-31m-en", "shared": false,
          "license": "Apache-2.0", "url": "https://huggingface.co/gradium/phonon-31m-en",
          "gated": false, "revision": "8c64b55e...", "download_bytes": 65049306,
          "on_disk_bytes": 65049306, "downloaded": true,
          "previous": [{"revision": "...", "bytes": 64000000}],
          "refs": ["main"], "in_use": true,
          "quality": 5, "speed": 5,
          "note": "Runs on the CPU, leaving the GPU to speech to text; 38 English voices",
          "recommended": false
        }
      ],
      "shared": [{"id": "gradium/phonon-voices", "shared": true, "...": "as in models"}]
    }
  ]
}
```

`id` is what `stt.model` and `tts.model` hold and what `download` and
`delete` take: the model's Hugging Face repository, unless several models
are folders of one repository. `downloaded` means every locked file is in
the snapshot at the pin;
`previous` lists the older pinned revisions in the cache with their size;
`refs` the branches pointing at the pin; `in_use` follows the config file.
`quality` and `speed` are 1 to 5, from the model's benchmark on the
reference Mac ([benchmarks.md](benchmarks.md), ADR 0024), or null when it
has not been measured; `note` is one line on what the model is for, possibly
empty; `recommended` is always present: true on each default engine's
first model (what a new install runs), false otherwise.
`shared` entries have the same fields as `models`. `downloaded` also checks
each file's size against the lock, so a truncated file reads as missing.
`on_disk_bytes` is the whole cached revision: models that are folders of
one repository each show the same revision, while `total_bytes` counts it
once. A plugin engine voiced does not catalogue is listed with no models.
`unavailable_reason` says why an installed engine cannot run on this Mac,
as a phrase that follows its name ("needs macOS 15 or later" for Moonshine
on macOS 14), and is null for one that can, and for one that is not
installed; the unavailable engine's models are still listed, so a download
can be deleted, the table's state says "cannot run here", and `download`
refuses them.

`voiced models download <model> --json-progress` writes ADR 0018's events,
one JSON object per line on stdout (human output goes to stderr): `hello`,
then `download` (`model`, the model's id; `bytes`; `total`, from the lock)
at most four times a second per model, its shared models after it, then
`done`, or `error` with `code`, `message` and the `repo` (the model's id),
`revision` and `path` it was on. Exit 0 with `done`, 1 with `error`. SIGTERM
(Cancel) ends it at once, with an `error` and exit 143, without waiting
for the files in flight; the next download resumes the partial files. The
codes:

| `code` | Meaning |
|---|---|
| `token_missing` | Hugging Face answered 401 and there is no token (`hf auth login`). |
| `token_invalid` | 401 with a token: Hugging Face rejected it. |
| `gated_terms` | 403 on a gated model: accept its terms on its Hugging Face page. |
| `network` | Hugging Face could not be reached. |
| `disk` | Less free space than the bytes still to fetch plus 1 GB, checked first. |
| `busy` | An install, update or uninstall holds the state directory. |
| `failed` | Anything else, a repository that does not exist included; the message says what. |

`voiced models delete <model> --yes --json` (`--json` needs `--yes`) prints
what it deleted, with exit 0; `deleted` is empty when nothing was cached.
Any refusal or failure is `{"error": {"code": ..., "message": ...}}` with
exit 1: `in_use` for the model the config uses (or one in the same
download as it), else `busy` or `failed`.

```json
{"model": "gradium/phonon-31m-en",
 "deleted": [{"repo": "gradium/phonon-31m-en", "revision": "8c64b55e...", "bytes": 65049306},
             {"repo": "gradium/phonon-voices", "revision": "da6c9ec0...", "bytes": 9510439}],
 "freed": 74559745,
 "notes": ["Phonon's shared files (gradium/phonon-voices) are deleted with it."]}
```

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
environment, downloads the models into the Hugging Face cache, and loads
the LaunchAgents. A warning about `HF_TOKEN` along the way is harmless:
the models are public and need no Hugging Face account.
`voiced install --source ~/voiced --dry-run` shows everything it would do
without writing anything.

To build the menu bar app from the same checkout, run
`~/voiced/menubar/Scripts/bundle.sh` and open
`~/voiced/menubar/build/voiced.app`. It needs Swift 6 (Xcode 16 or newer);
see [menubar/README.md](../menubar/README.md).

## launchd

Three LaunchAgents in your own launchd domain, `gui/<uid>`, labels
`io.github.julianbeaulieu.voiced.stt`, `.tts` and `.watchdog`, plists in
`~/Library/LaunchAgents/io.github.julianbeaulieu.voiced.{stt,tts,watchdog}.plist`
([ADR 0035](decisions/0035-launch-agents.md)). `[launchd] label_prefix`
changes the labels `voiced install` writes and that `voiced status`,
`start`, `stop`, `restart`, `doctor` and the watchdog address.

Each plist sets:

| Key | Value | Why |
|---|---|---|
| `ProgramArguments` | `~/.voiced/bin/voiced serve <service>`, or `watchdog` | The entry point `voiced install` links ([ADR 0017](decisions/0017-homebrew-packaging.md)). |
| `RunAtLoad` | true | Start at login, and when `voiced install` or `voiced start` loads it. |
| `KeepAlive` | `{PathState: {~/.voiced/bin/voiced: true}}` | Restart on exit while the entry point exists; not after `brew uninstall voiced` left it dangling. |
| `ThrottleInterval` | 10 | Wait 10 s between restarts so a crash loop does not spin. |
| `ProcessType` | Interactive | Full CPU and GPU scheduling priority. |
| `WorkingDirectory` | your home | |
| `EnvironmentVariables` | `HOME`, `PATH` (service environment first), `VOICED_CONFIG`, `PYTHONUNBUFFERED`, nothing else | The service reads the same config file as the CLI, and takes the model cache (`[install] hf_home`), the espeak-ng paths (`[tts] espeak_library`, `espeak_data`) and the rest from it. |
| `StandardOutPath`, `StandardErrorPath` | `<log_dir>/<service>.stdout.log`, `.stderr.log` | launchd opens them as you, appending, before the service starts, so a crash during startup is kept ([Logs](#logs)). |
| `Umask` | 0o077 (63) | Every file the service creates, those two included, is readable only by you. |
| `AssociatedBundleIdentifiers` | the menu bar app | System Settings > General > Login Items lists the services under voiced's name. |

There is no `UserName`: an agent runs as the user whose domain it is in.

Agents run while you are logged in: they start at login and stop at
logout ([After a reboot](#after-a-reboot)). Metal works from an agent: the
probe saw the GPU from one on macOS 15.7 with MLX 0.32. Run
`voiced doctor --metal-probe` to confirm it on your machine and macOS
version; `voiced doctor` only accepts a probe result recorded by a
LaunchAgent on the macOS version that is running now.

If System Settings > General > Login Items > Allow in the Background has
voiced switched off, launchd does not start the services; the menu bar app
says so.

Useful launchctl commands, with the default prefix, as yourself:

```sh
launchctl print gui/$(id -u)/io.github.julianbeaulieu.voiced.tts        # state, pid, last exit
launchctl kickstart -k gui/$(id -u)/io.github.julianbeaulieu.voiced.tts # force restart
launchctl bootout gui/$(id -u)/io.github.julianbeaulieu.voiced.tts      # unload until login
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/io.github.julianbeaulieu.voiced.tts.plist
```

### Moving from LaunchDaemons

voiced 0.4.0 and earlier installed the services as LaunchDaemons in
`/Library/LaunchDaemons`, which only an administrator can remove, and the
agents cannot run beside them (both would use the same ports). The first
`voiced install` of a newer release therefore stops before it changes
anything and prints one line to run, the last time voiced needs a
password:

```sh
sudo /bin/sh -c '/bin/launchctl bootout system/io.github.julianbeaulieu.voiced.stt 2>/dev/null; /bin/launchctl bootout system/io.github.julianbeaulieu.voiced.tts 2>/dev/null; /bin/launchctl bootout system/io.github.julianbeaulieu.voiced.watchdog 2>/dev/null; /bin/rm -f /Library/LaunchDaemons/io.github.julianbeaulieu.voiced.stt.plist /Library/LaunchDaemons/io.github.julianbeaulieu.voiced.tts.plist /Library/LaunchDaemons/io.github.julianbeaulieu.voiced.watchdog.plist'
voiced install
```

It stops the three daemons and deletes their plists, nothing else; your
`~/.voiced`, config and models stay. The services are down from the first
command until the second has loaded the agents, a few seconds with the
models already cached. The second needs you logged in at the Mac (in
person or with Screen Sharing; an SSH login alone is not enough): over
SSH, plan the move with someone at the Mac, or the services stay down
until they log in and run `voiced install`. `voiced start`, `stop`,
`uninstall`, `status` and `doctor` print the same message while the
daemons are there. They are found by the default labels, the only ones
the old root step installed, both by their plists and by `launchctl print
system/<label>`, so one still loaded after its plist was deleted is found
too.
The menu bar app does the same with one administrator prompt when it
updates or repairs such an install ([The menu bar app](#the-menu-bar-app)).

## Watchdog

`KeepAlive` only restarts a process that exits. MLX can wedge a process that
stays alive: the GPU queue never drains while the process keeps running. The
watchdog agent catches that. Every `interval_s` it requests `/health` from
each service with a `timeout_s` limit and applies these rules:

| Observation | Action |
|---|---|
| `ok` | Reset the failure count; end the startup grace period. |
| `idle` (the model unloaded after `idle_unload_s`) or `loading` (loading it again for a request) | As `ok`. A load that hangs holds the queue, which the stall rule below catches. A service with `idle_unload_s` set gets no probe, which would keep its model loaded. |
| `off`: launchd's pid for the service is its parked process (`enabled = false`) | Nothing: never polled, never a failure, no grace; logged once per process. |
| Connection refused or timed out within `startup_grace_s` of the watchdog starting, of its last restart of the service, or of launchd giving the service a new pid, and before that process has answered with its model loaded | Nothing. The service is starting. |
| `starting` while the service's own uptime is below `startup_grace_s` | Nothing. The model is loading. |
| `starting` beyond `startup_grace_s` | Restart: the load is hung. |
| `degraded` | Start a timer; restart once it exceeds `degraded_window_s`. |
| `missing_model` | Nothing; logged once per process. A restart cannot download the model. |
| Queue not empty for `stall_after_s` with no inference completing | One failure: stalled. |
| Optional probe request times out after `probe_timeout_s` | One failure: stalled. |
| Connection refused or timed out after the grace period (or after the process has answered with its model loaded), or a non-health answer | One failure. |
| `failures_before_restart` consecutive failures | Restart. |
| The agent is not loaded (after `voiced stop`) | Never restart; logged once. |

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
throttle interval. The watchdog is an agent in the same domain, so it may
signal the services and kickstart them. A second restart of the same service within
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

Once voiced is installed, the watchdog agent already serves the socket:
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
| `status` | | `installed`, and per service (`stt`, `tts`, `watchdog`): `state` (`running`, `starting`, `stopped`, `degraded`, `missing_model` (the model is not downloaded; `model` names it), `off` (switched off: the process is parked), `unknown`), `pid`, `health` (the `/health` body, whose `status` says `idle` while the model is unloaded and `loading` while it loads again), `engine`, `model`, `warm` (the first load is done; true while `idle` too), `uptime_s`, `restarts` |
| `settings.get` | | `bind` (`loopback` or `lan`), `api_key_set`, `zeroconf`, `stt`, `tts`, `browser_settings`, `ports`, `paths`. Never the API key. |
| `settings.set` | any of `bind`, `api_key`, `zeroconf`, `browser_settings`, `stt.engine`, `stt.model`, `tts.engine`, `tts.model`, `tts.voice`, `tts.speed`, `stt.enabled`, `tts.enabled`, `stt.idle_unload_s`, `tts.idle_unload_s` | the new settings plus `changed`, `restart_required` and `warnings` |
| `engines.list` | | installed engines with their `label`, models, languages, `model_languages` (each model's, which the app shows for the model in use), `unavailable_reason` (why the engine cannot run on this Mac, such as Moonshine's "needs macOS 15 or later" below macOS 15; null when it can), voices, `voice_labels`, `voice_languages` and `voice_names` (each voice's personal name, for engines whose voices have one), without loading any |
| `service.restart` | `service`: `stt`, `tts` or `all` | `accepted` at once and `detail` per service; progress follows as `status` events |
| `logs.tail` | `service`: `stt`, `tts` or `watchdog`; `lines` up to 1000 | `lines`, each cut at 4 KiB (` [line truncated]`), from at most the last 4 MiB of the file |
| `open` | `what`: `status_page`, `logs_dir` or `config_file` | `target`, a URL or path for the client to open |
| `tts.speak` | `text` (up to 60 characters) and `voice` | `format` (`wav`) and `audio`, a base64 WAV file: the line spoken by the running tts service at its default speed, for hearing a voice before choosing it. Answered in its own task, so the connection's other requests do not wait for it; cancelled, and the synthesis stopped, when the client closes the connection. Waits up to 30 s (a model unloaded while idle loads first). Audio over 10 s, a voice the service does not serve, or a 4xx from it is `bad_request`; no answer or a 5xx is `service_unavailable` |

`settings.set` writes the config file atomically and changes only the
values it sets, so comments and layout stay. It never restarts anything:
`restart_required` says which services need `voiced restart` for the change
to apply (`tts.*` the tts service, `stt.*` the stt service, `bind`,
`api_key` and `zeroconf` both; `browser_settings` and `tts.speed`, which
are read from the file as they are used, neither). Changing
`bind` to `lan` also starts the zeroconf announcement of the Wyoming
listeners after that restart, if `zeroconf` is on
([ADR 0011](decisions/0011-wyoming.md)). Switching `stt.engine` or
`tts.engine` without giving a model selects the new engine's own model (and
voice, if the old one does not exist there). Any registered engine can be
selected; `voiced install` installs the extras they need. A file holding an
API key is made readable by your user only. Invalid values are refused with
`invalid_setting` and the field name, and the file is left as it was.
A change of engine or model to a catalogued model that is not downloaded is
refused with `model_not_downloaded`, with `field` and `model`, and a message
naming the `voiced models download` command. Errors are `not_installed`,
`invalid_setting`, `model_not_downloaded`, `unknown_op`, `bad_request`,
`service_unavailable` and `internal`.

`[server] browser_settings = true` adds the same settings as a form on each
status page. It always needs the API key, so it is refused until a key is
set, on loopback too: any web page you open could otherwise reach it.

## Logs

| File | Contents |
|---|---|
| `~/Library/Logs/voiced/stt.log`, `tts.log` | Service log, rotated at 10 MB, five backups kept. |
| `~/Library/Logs/voiced/watchdog.log` | Every restart and every non-healthy observation; healthy polls only at `debug`. |
| `~/Library/Logs/voiced/<service>.stderr.log` | The service's stderr, opened by launchd. Log records are not written here under launchd, only output that bypasses logging, such as the traceback of a crash during startup; `voiced logs --stderr` includes it. Not rotated, but stays small. |
| `~/Library/Logs/voiced/<service>.stdout.log` | The service's stdout; normally empty. Not rotated. |

`log_level` under `[server]` takes `debug`, `info`, `warning`, `error`.
`log_dir` under `[server]` moves all of them: the rotated logs at the
services' next start, the stdout and stderr files once `voiced install`
has rewritten the plists that name them. If launchd cannot open one of
those two, it does not start the service, and `launchctl print
gui/$(id -u)/<label>` shows `last exit code = 78: EX_CONFIG`.
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

## After a reboot

The services are your LaunchAgents: they start when you log in and stop
when you log out. Locking the screen, display sleep and switching to
another user (fast user switching) keep them running. Whether a reboot
brings them back by itself depends on the login:

| Mac | After a reboot |
|---|---|
| FileVault on | voiced starts when you unlock the disk at boot; the unlock logs in the account that unlocked it, so on a Mac with several accounts, only if that is the one that installed voiced |
| FileVault off, automatic login as you | voiced starts at the automatic login |
| FileVault off, no automatic login | voiced waits at the login window until you log in |

`voiced doctor` says which row applies ("After a reboot"). A Mac that must
come back unattended, a remote reboot or a power cut, needs automatic login
(System Settings > Users & Groups > Automatically log in as; macOS offers it
only with FileVault off, and the disk is then readable by anyone with
physical access) or a person to unlock FileVault. voiced does not change
either setting.

## Memory budget

| Service | Model | Memory |
|---|---|---|
| tts | Kokoro-82M (default) | 469 MB peak resident |
| tts | Soprano-80M (opt-in) | 392 MB resident |
| tts | Phonon 31M (opt-in, on the CPU) | 530 MB resident (260 MB footprint) after a long reply |
| tts | Pocket TTS (opt-in, on the GPU) | 430 MB resident (680 MB footprint) after a long reply; 968 MB MLX peak while synthesizing |
| tts | Chatterbox Turbo 4-bit (opt-in) | 0.89 GB footprint after a long reply (fp16: 2.99 GB) |
| tts | OmniVoice (opt-in) | 1.59 GB MLX active, 3.19 GB MLX peak (2.14 GB footprint) after a long reply; 3.87 GB beside Parakeet, over the 3 GB budget for both |
| tts | Marvis 100M (opt-in) | 1.23 GB footprint, 2.31 GB MLX peak after use (benchmark row); 3.04 GB beside Parakeet in the bench.py run, over the 3 GB budget for both (250M: 1.45 GB, 3.23 GB) |
| stt | Parakeet TDT 0.6B v3 | 1.28 GB after load, 2.46 GB serving peak, 3.6 to 4.1 GB once while loading |
| stt | Qwen3-ASR 0.6B with the aligner (opt-in) | 2.29 GB after load, 3.75 GB serving peak on a 30 s turn |
| stt | Moonshine Small (opt-in, on the CPU) | 330 MB footprint (1.32 GB resident, mostly the memory-mapped model) after use; Tiny 177 MB and Medium 789 MB resident after load |
| stt | Nemotron 3.5 (opt-in) | 1.32 GB after load, 2.18 GB serving peak (MLX); 1.44 GB footprint after use |
| stt | Nemotron English (opt-in) | 1.28 GB after load in bf16, 2.16 GB serving peak (MLX); 1.39 GB footprint after use |
| stt | Whisper large-v3-turbo 8-bit (opt-in) | 0.87 GB after load, 1.72 GB serving peak (MLX); 1.01 GB footprint after use (fp16: 1.76 GB) |

All figures were measured on an M1 Max, each engine in its own process,
when the engine was added: resident set size, or MLX allocations where
named. [benchmarks.md](benchmarks.md) has every engine's current physical
footprint and MLX peaks, measured the same way for all. Parakeet's are MLX allocations
(`mx.get_peak_memory()`). Batch input longer than 20 s is transcribed in
overlapping 20 s chunks and MLX's buffer cache is emptied after each request,
so the serving peak holds for any input length. The load peak is a transient:
parakeet-mlx reads the fp32 weights and then casts them to bf16
([ADR 0008](decisions/0008-stt-engine-parakeet.md)).

What a service costs while it waits, with the defaults, `idle_unload_s` set, and
switched off (`voiced serve` with `/metrics.json`, M1 Max; see
[ADR 0033](decisions/0033-model-residency.md) for every engine):

| Service | Model loaded | Model unloaded (`idle`) | First request after `idle` | Off (`enabled = false`) |
|---|---|---|---|---|
| stt, Parakeet | 1.51 GB footprint, 2.1 GB after use | 0.12 GB | waits about 0.4 s for the load | 29 MB, no model, no ports |
| tts, Kokoro | 0.47 GB footprint, 0.73 GB after use | 0.10 GB | waits about 0.25 s for the load | 29 MB, no model, no ports |

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
  `sudo pmset -a sleep 0 disksleep 0 autorestart 1 womp 1`, and keep its
  user logged in or log in automatically ([After a reboot](#after-a-reboot)).
- Exclude the Hugging Face cache from Spotlight under **System Settings**,
  **Siri and Spotlight**, **Spotlight Privacy**.

## Updating

The plists run `~/.voiced/bin/voiced`, a link to the active environment,
and are the same for every way of installing, so an update that only
changes the environment does not reload them. The watchdog restarts both
services onto the new environment and then itself, within one poll
(`[watchdog] interval_s`) of either of two things: a file it runs from
was deleted or replaced (the old Homebrew keg or Python removed, a keg
rebuilt by `brew reinstall`, an old `envs/<id>` pruned), or
`~/.voiced/bin/voiced` was repointed (`voiced install`, the app). While
Homebrew is installing voiced or its Python, the watchdog neither polls
nor restarts anything, and refuses `voiced restart` (it exits non-zero
with the reason), for up to 30 minutes.
`voiced restart all` does it at once. If the changelog says a release
changes the plists, run `voiced install` again: it reloads the agents
whose plists changed.

### The menu bar app

The app updates itself (ADR 0034), from 0.5.0 on: an app of 0.4.0 or
earlier is updated to 0.5.0 by hand, with Homebrew or from the release
page, and the versions after it arrive in the app. It checks when it opens
and once a day,
and **Install and Relaunch** on the card in its menu installs the new
version and opens it again; **Check for Updates…** checks at once. The
relaunched app then updates the services to the voiced it carries, in the
installer window, without asking again. For an install Homebrew or the
command line manages, it shows the command instead. Turn the checks off in
Settings > General > Check for updates automatically. `brew update` and
`brew upgrade --cask voiced`, and a new download from the
[release page](https://github.com/JulianBeaulieu/voiced/releases/latest)
still work; after either the app offers **Update voiced** on its next
launch.

No step asks for an administrator password: the first open installs the
services, **Update voiced**, Repair, Roll back and Uninstall run without
one, and so do `brew upgrade --cask voiced` and a reinstall from 0.5.0 on,
which leave the services running. **Install and Relaunch** asks only if the app's
folder is not writable for your account; then macOS asks once to replace
it. Homebrew asks only if `/Applications` is not writable for your account
(a managed Mac).

The one exception is once, on a Mac that ran 0.4.0 or earlier, whose
services are still LaunchDaemons ([Moving from LaunchDaemons](#moving-from-launchdaemons)).
**Update voiced** (or Repair) builds the new environment first, then asks
to "remove the old background services (the last time a password is
needed)", then installs the services as LaunchAgents. Cancel, and the
services keep running the old way until you accept. If the first
`brew upgrade --cask voiced` from 0.4.0 or earlier already asked Homebrew's
password (it runs the old cask's uninstall stanza, which removed the
daemons), the services are down from that upgrade until you open the app,
which then asks nothing and installs the agents.

`brew uninstall --cask voiced` removes the app only; the services keep
running from `~/.voiced`. To remove them:

- before it, use Uninstall in the app's Settings;
- or run `brew uninstall --zap --cask voiced`, which stops and removes the
  LaunchAgents and their plists and the app's uv cache, without a
  password, and leaves `~/.voiced`, the config and the models (services
  still on LaunchDaemons from 0.4.0 or earlier need the removal line in
  [Moving from LaunchDaemons](#moving-from-launchdaemons));
- or afterwards, from a terminal: `~/.voiced/bin/voiced uninstall --yes`
  (add `--remove-config`, `--remove-models` and `--remove-logs` to delete
  those as well).

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

After the first `brew upgrade voiced` from 0.4.0 or earlier, run
`voiced install` once: it prints the line that removes the old
LaunchDaemons ([Moving from LaunchDaemons](#moving-from-launchdaemons)).
Until then they run the new version, whose watchdog looks for the services
among your LaunchAgents and so does not restart a hung one.

To remove voiced, run `voiced uninstall --yes` first, then
`brew uninstall voiced`. Homebrew does not show the caveats at uninstall.
Uninstalling the formula first leaves the agents loaded with
`~/.voiced/bin/voiced` pointing nowhere; launchd
then stops restarting them (their `KeepAlive` holds only while that path
exists) and `voiced doctor` reports the dangling link. Reinstall the formula
to bring them back, or run the uninstall steps above. If an agent stays
loaded but not running after the link is back (`voiced doctor`: "loaded
but not running"; launchd can leave such a job at `spawn scheduled`),
`voiced stop` and `voiced start` it: that unloads and loads it again,
which starts it.

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
repoints the link; the services keep running the previous environment until
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
`voiced restart all`. Nothing to delete by hand:

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
running services still use the removed venv's files.

`voiced restart all` restarts stt and tts, then the watchdog, and reports
once the new watchdog answers on the control socket. `voiced restart stt`
or `tts` leaves the watchdog running its old code; `voiced restart
watchdog` restarts it alone.

Read [CHANGELOG.md](../CHANGELOG.md) for anything that needs a manual step.
