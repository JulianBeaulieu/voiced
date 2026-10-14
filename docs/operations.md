# Operations

Running voiced as a service on a Mac that nobody logs in to.

## Install and lifecycle

| Command | What it does |
|---|---|
| `voiced install` | Creates the service environment at a short path, downloads the models, renders the plists and prints a sudo script that copies them to `/Library/LaunchDaemons` and loads them. |
| `voiced start`, `voiced stop` | `launchctl kickstart` and `launchctl kill` for both services. <!-- TODO(phase2): verify the exact launchctl calls --> |
| `voiced status` | Both services' launchd state and `/health`. |
| `voiced logs [stt\|tts\|watchdog]` | Tails the log files. |
| `voiced doctor` | Checks the machine (see below). |
| `voiced uninstall` | Prints the sudo script that unloads and removes the plists, then removes the environment and models on request. |
| `voiced serve stt\|tts` | Runs one service in the foreground. For development and for checking an engine before installing. |

voiced never runs root steps itself. Anything that touches
`/Library/LaunchDaemons` is printed as a script for you to read and run with
`sudo`. The script is short; read it.

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
runs). `voiced doctor` repeats that probe from a daemon-like context so you
can confirm it on a new macOS version before relying on it. If a future
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
(`failures_before_restart`) it runs `launchctl kickstart -k` on that service.
A failure is a timeout, a refused connection, or a service stuck in
`starting` well past its usual load time. `degraded` alone is not a failure:
one bad request should not restart a working model.

The watchdog exists because `KeepAlive` only restarts processes that exit.
MLX can wedge a process that stays alive and stops answering; `kill -9` is
handled by launchd within the throttle interval, a hang is handled by the
watchdog within roughly `interval_s * failures_before_restart` plus load time.

The watchdog needs to run `launchctl kickstart -k` on system daemons, which
requires root. <!-- TODO(phase2): verify how the watchdog gets that privilege -->

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

Resident memory per service with the default models, measured on an M1 Max:

| Service | Model | Resident |
|---|---|---|
| tts | Kokoro-82M (plus Soprano-80M if loaded) | about 0.55 GB for both |
| stt | Parakeet TDT 0.6B v3 | pending <!-- TODO(phase2): measure --> |

Plan for the two services together to stay under 3 GB. On a Mac that also
runs an LLM server, add up what each resident model needs: macOS itself
takes about 4 GB, a pinned 8B model in Ollama about 6.5 GB, container
runtimes whatever you cap them at. On a 32 GB machine that still leaves
headroom; on 16 GB, run one LLM at a time.

Apple Silicon shares memory between CPU and GPU, so there is no separate
VRAM to run out of, but the GPU can only wire a fraction of total memory by
default (about two thirds). Models that do not fit in the wired limit page
and get slow.

## Optional: raise the GPU wired limit

If the Mac also runs large LLMs, you may want to let the GPU wire more memory.
This is a system setting, not a voiced feature, and it resets at reboot, so
it needs a LaunchDaemon of its own. The value is in megabytes; leave at least
8 GB for macOS and the CPU side.

Create `/Library/LaunchDaemons/local.gpu-wired-limit.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>local.gpu-wired-limit</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/sbin/sysctl</string>
        <string>iogpu.wired_limit_mb=24576</string>
    </array>
    <key>RunAtLoad</key><true/>
</dict>
</plist>
```

Then:

```sh
sudo chown root:wheel /Library/LaunchDaemons/local.gpu-wired-limit.plist
sudo launchctl bootstrap system /Library/LaunchDaemons/local.gpu-wired-limit.plist
sysctl iogpu.wired_limit_mb
```

Pick a value for your machine: 24576 (24 GB) suits a 32 GB Mac that runs an
8B LLM next to voiced. Set it to `0` to restore the default.

## Keeping a headless Mac up

Settings voiced does not touch but a server benefits from, all under
**System Settings**, **Energy** (or via `pmset`):

```sh
sudo pmset -a sleep 0 disksleep 0 autorestart 1 womp 1
```

`autorestart` brings the Mac back after a power failure, `womp` enables wake
on LAN. Spotlight indexing of the model cache is pointless and can be turned
off for that folder under **Siri and Spotlight**, **Spotlight Privacy**.

## Updating

```sh
voiced stop
uv tool upgrade voiced          # or pipx upgrade voiced
voiced install                  # re-renders plists, keeps models and config
voiced start
```

<!-- TODO(phase2): verify the upgrade path with the CLI worker -->

The config file and the Hugging Face cache survive upgrades. Read
[CHANGELOG.md](../CHANGELOG.md) for anything that needs a manual step.
