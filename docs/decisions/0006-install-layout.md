# 0006: Install layout

Date: 2026-10-14. Status: accepted.

## Context

`voiced install` has to work for people who are not the author, on a Mac
that may have Homebrew, `uv`, both or neither, and it must not take root
silently. The daemons run as the installing user, so everything they need
must be readable by that user without root, and the user must be able to
update it without root.

The venv path length is not a constraint. espeak-ng does break on long data
paths, but only the copy bundled in the `espeakng-loader` wheel lives inside
the venv, and the Kokoro engine does not use it: it loads the Homebrew keg
or the `VOICED_ESPEAK_LIBRARY` override (ADR 0009). The other usual limit,
the kernel's 512-byte `#!` line, does not apply either: uv and pip both
write a `/bin/sh` trampoline instead of a `#!` interpreter line when the
interpreter path is long or contains spaces (checked with uv 0.9 and pip 26
on a 743-character venv path). An earlier installer warned above 80
characters; that warning was removed.

## Decision

| What | Where | Why |
|---|---|---|
| venv | `~/.voiced` (`[install] venv_dir`, `--venv`) | short, user-owned, no sudo. `/usr/local/voiced` was rejected: it is root-owned on Apple Silicon Macs, so creating and upgrading it would need sudo, which the installer never runs. |
| config | `~/.config/voiced/config.toml` | copied from the packaged `config.example.toml` on first install, never overwritten |
| Metal probe script | `~/.config/voiced/metal-probe.sh` | written by `voiced doctor --metal-probe`, its plist inside it. Nothing else is staged for root: the daemons' plists are rendered as root by the privileged script (ADR 0018), and `install` removes what earlier releases staged in `~/.config/voiced/launchd/` |
| logs | `~/Library/Logs/voiced/` (`[server] log_dir`) | the macOS convention, visible in Console.app |
| models | Hugging Face cache (`~/.cache/huggingface` or `[install] hf_home`) | not vendored; the services set `HF_HOME` from the config |
| installed plists | `/Library/LaunchDaemons/<label>.plist`, root:wheel 0644 | written only by the privileged script, run through the printed `sudo` line or by the menu bar app |

The installer uses `uv` when it is on `PATH` (`uv venv --python 3.12`, then
`uv pip install`; uv downloads a Python if needed) and falls back to
`python3 -m venv` plus `pip`. The package spec is the dev checkout when
`voiced` is imported from one (so `uv run voiced install` in the repo
installs that tree), otherwise `voiced==<version>` from PyPI, always with the
`[stt,tts]` extras. The MLX extras are large (about 680 MB installed).

Models are fetched by running, inside the new venv, a script that
instantiates each configured engine and awaits `load()`; if an engine plugin
is not installed it falls back to `huggingface_hub.snapshot_download` for the
configured model id. Running this in the venv matters because the installing
interpreter (for example `uvx`) does not have MLX.

`install --dry-run` prints the plan, every command and the root step, and
writes nothing. The real run writes the config (if absent), the log dir and
the environment, then prints the root step and stops: one line,
`sudo /bin/bash <environment>/.../voiced/launchd/voiced-privileged.sh install
<user>` (ADR 0018).

`uninstall` prints the matching `uninstall <user>` line first and refuses
to delete anything while any label is still loaded (otherwise launchd would
respawn a missing binary every 10 s). Deleting needs `--yes`; models, logs
and config each need their own flag. Only one kind of directory is deleted
recursively: the venv (only if it contains `pyvenv.cfg`). The models are
deleted through `huggingface_hub` (`scan_cache_dir().delete_revisions`), and
only the revisions voiced pinned: `voiced.models.PINS` with their `previous`
revisions, and what `python -m voiced.models` lists in each environment
uninstall finds. `huggingface_hub` 2.x keeps file bytes in a store shared by
every repository in the cache (`hub/blobs/<2 hex>/<xet hash>`), and a
repository's folder holds only links into it, so removing the folder would
leave the bytes behind (amended 2026-10-20; the first version did that).
Other tools' revisions in the same cache stay, with their blobs, and so does
a configured model voiced does not pin. Everything else is deleted file by file, because
the directories may be shared: `log_dir = "~/Library/Logs"` and
`VOICED_CONFIG=~/config.toml` (an earlier release's staging dir `~/launchd`) are legal
configurations. `--remove-logs` removes the per-service log files, their
rotations and the Metal probe result; `--remove-config` removes the config
file, the staged plists and the generated scripts. A directory is removed
afterwards only if that left it empty. Every recursive delete is also
guarded against the filesystem root, the home directory and its ancestors,
and symlinks.

### Logs

Each service writes its log through a `RotatingFileHandler` to
`<log_dir>/<service>.log` (10 MB, five backups). Stdout and stderr go to
`<service>.stdout.log` and `<service>.stderr.log`, which nothing rotates,
so a service that logged every line to stderr as well would grow it
without bound and duplicate the rotated log. `configure_logging`
therefore adds a stderr handler only when there is no log directory or
stderr is a terminal (`voiced serve` run by hand, which shows every line).
Under launchd stderr is a file, so no log records go there; the stderr file
keeps only what bypasses logging: a traceback from a crash during import or
before logging is configured, and errors raised by the logging system
itself. The watchdog decides by mode instead (ADR 0012): as a daemon it
writes only its rotating file, and with `--once` only stderr; both
configurers share the size and backup count from `voiced.launchd.plist`.
`voiced logs` reads the rotated `<service>.log`; `voiced logs --stderr`
adds the stderr file when looking for a crash.

The plists do not use `StandardOutPath`/`StandardErrorPath` for those two
files. launchd may open or create them as root before it drops to
`UserName`, and they live in a directory the user owns, so a process
running as the user could replace one with a symlink to a root-only path,
make the daemon exit, and have launchd open that path as root on the
restart. Instead the plist sets `VOICED_STDIO_LOG=<log_dir>/<service>`, and
the process opens both files itself, as the user, first thing
(`voiced.entry`, before the CLI is imported, so an import traceback is
kept; `voiced.launchd.metal_probe` does the same). Each is opened
`O_APPEND|O_CREAT|O_NOFOLLOW`, set to 0600 (also a file an old plist had
launchd create), and must be a regular file;
then `dup2` puts them on fds 1 and 2, so C-level output (MLX, espeak-ng)
lands there too. If fd 1 or 2 was closed at startup the open reuses it and
that descriptor is kept. A file that cannot be opened is reported on the
original stderr (`/dev/null` under launchd) and logged as a warning in
`<service>.log` once logging is configured; the daemon runs on without the
redirect rather than crash-looping. Plists rendered before this change keep the two keys, and
the old exposure, until the root step replaces them.

`HF_HOME` comes from `[install] hf_home`, not from the plists: every
`voiced` command sets it, and the espeak-ng variables, from the config file
alone and clears what the shell exported (ADR 0018). The services and the
installer's model download, its child, therefore see the same cache, so
the daemons find the weights the installer fetched.

Every path that ends up in a generated shell script (the Metal probe's) is
quoted with `shlex.quote`, and `[launchd] label_prefix` is validated to
letters, digits, dots and dashes, because that script runs as root.
Re-running the root step boots the old instance out and waits for launchd
to finish tearing it down before bootstrapping again; `bootout` is
asynchronous and an immediate `bootstrap` fails.

## Consequences

- Upgrading is `voiced install` again (ADR 0017: a new environment, the
  link repointed); the plists do not change, so it needs no root step.
- The plists name `~/.voiced/bin/voiced` only (ADR 0018), so a state
  directory elsewhere gets no daemons.
- `voiced doctor` reports the venv's Metal device using the venv's Python,
  not the interpreter running `doctor`.

## Amendment (ADR 0017)

`~/.voiced` is the state directory, not necessarily a venv: `bin/voiced`
links to the active environment, which `voiced install` takes from the
`voiced` it runs as, or builds with `--source` into `~/.voiced/envs/<id>`
from the shipped lock (no more upgrade in place, no pip fallback: `--source`
needs uv). `install.json` marks the directory as voiced's. `uninstall`
removes only what that layout owns and keeps anything else in the
directory, instead of deleting a directory that has `pyvenv.cfg`. ADR 0017
has the rules.
