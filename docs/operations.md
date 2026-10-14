# Operations

Running voiced on a Mac that nobody logs in to. "Service" below means the
stt or tts process; "daemon" means the launchd job that keeps it running.

## Install and lifecycle

| Command | What it does |
|---|---|
| `voiced install` | Creates the service environment at `~/.voiced` (a short path, because espeak-ng breaks on long ones), downloads the models, renders the plists into `~/.config/voiced/launchd/` and prints the one root step: `sudo bash ~/.config/voiced/launchd/install-daemons.sh`. |
| `voiced start`, `voiced stop` | Start or stop both services (or one, by name) through `launchctl`. |
| `voiced status` | Both services' launchd state and `/health`. |
| `voiced logs [stt\|tts\|watchdog]` | Tails the log files. |
| `voiced doctor` | Checks the machine (see below). |
| `voiced uninstall` | Prints the root step that unloads and removes the plists, then removes the environment and models on request. |
| `voiced serve stt\|tts` | Runs one service in the foreground. For development and for checking an engine before installing. |

<!-- TODO(phase2): verify the command table against the merged CLI -->

voiced never runs root steps itself. Anything that touches
`/Library/LaunchDaemons` is printed as a short script for you to read and
run with `sudo`.

## launchd

Three LaunchDaemons, labels `<prefix>.stt`, `<prefix>.tts` and
`<prefix>.watchdog`. The default prefix is `io.github.julianbeaulieu.voiced`;
change it with `[launchd] label_prefix` before installing if you need a
different namespace.

Each plist sets:

| Key | Value | Why |
|---|---|---|
| `UserName` | the installing user | The models and config live in that user's home. |
| `RunAtLoad` | true | Start at boot. |
| `KeepAlive` | true | Restart on exit, whatever the exit status. |
| `ThrottleInterval` | 10 | Wait 10 s between restarts so a crash loop does not spin. |
| `StandardOutPath`, `StandardErrorPath` | `~/Library/Logs/voiced/<service>.std*.log` | Catch output before logging is configured. |
| `ProcessType` | Interactive | Full CPU and GPU scheduling priority. |

Daemons rather than LaunchAgents because agents only run inside a logged-in
GUI session. Metal is available from a daemon context on Apple Silicon; this
was verified on macOS 15 with MLX 0.32 (GPU device visible, matrix multiply
runs). `voiced doctor` can repeat that probe from a real daemon context so
you can confirm it on a new macOS version before relying on it. If a future
macOS breaks it, the fallback is a LaunchAgent plus auto-login, which the
installer does not do today.

Useful launchctl commands, with the default prefix:

```sh
sudo launchctl print system/io.github.julianbeaulieu.voiced.tts   # state, pid, last exit
sudo launchctl kickstart -k system/io.github.julianbeaulieu.voiced.tts   # force restart
sudo launchctl bootout system/io.github.julianbeaulieu.voiced.tts  # unload
```

## Watchdog

The third daemon polls `/health` on both services every 15 s (config
`[watchdog] interval_s`). After three consecutive failures
(`failures_before_restart`) it restarts that service with
`launchctl kickstart -k`. A failure is a timeout, a refused connection, or a
service stuck in `starting` well past its usual load time. `degraded` alone
is not a failure: one bad request should not restart a working model.

<!-- TODO(phase2): verify the restart rules, the config keys and how the watchdog gets permission to kickstart system daemons -->

The watchdog exists because `KeepAlive` only restarts processes that exit.
MLX can wedge a process that stays alive and stops answering; `kill -9` is
handled by launchd within the throttle interval, a hang is handled by the
watchdog within roughly `interval_s * failures_before_restart` plus load time.

## Logs

| File | Contents |
|---|---|
| `~/Library/Logs/voiced/stt.log`, `tts.log`, `watchdog.log` | Application log, rotated at 10 MB, five backups kept. |
| `~/Library/Logs/voiced/<service>.stdout.log`, `.stderr.log` | Whatever escaped the logger, including Python tracebacks during startup. |

`log_level` under `[server]` takes `debug`, `info`, `warning`, `error`.
`debug` logs every request with its duration and queue wait, and the audio
parameters Wyoming clients send.

`voiced logs tts` tails the application log. For everything at once:

```sh
tail -f ~/Library/Logs/voiced/*.log
```

The logs contain transcripts and synthesized text at `debug` level. Keep the
level at `info` if that matters to you.

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

Resident memory per service with the default models:

| Service | Model | Resident |
|---|---|---|
| tts | Kokoro-82M (plus Soprano-80M if loaded) | about 0.55 GB for both |
| stt | Parakeet TDT 0.6B v3 | pending |

<!-- TODO(phase2): fill in the stt figure and re-measure both from the packaged services -->

Plan for the two services together to stay under 3 GB. On a Mac that also
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

```sh
voiced stop
uv tool upgrade voiced          # the CLI
voiced install                  # re-creates the service environment, keeps models and config
voiced start
```

<!-- TODO(phase2): verify the upgrade path against the merged installer -->

The config file and the Hugging Face cache survive upgrades. Read
[CHANGELOG.md](../CHANGELOG.md) for anything that needs a manual step.
