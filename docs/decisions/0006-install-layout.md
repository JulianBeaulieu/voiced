# 0006: Install layout

Date: 2026-10-14. Status: accepted.

## Context

`voiced install` has to work for people who are not the author, on a Mac
that may have Homebrew, `uv`, both or neither, and it must not take root
silently. Two concrete constraints:

- **espeak-ng breaks on long paths.** Kokoro's phonemizer path goes through
  espeak-ng, whose data-directory handling uses a fixed-size path buffer.
  With the venv under a long directory (a nested project checkout, a
  worktree, anything under `Library/Application Support`) phonemization
  fails. This was reproduced while benchmarking the engines before this
  project started; a short, fixed venv path avoids it.
- **The daemons run as the installing user**, so everything they need must be
  readable by that user without root, and the user must be able to update it
  without root.

## Decision

| What | Where | Why |
|---|---|---|
| venv | `~/.voiced` (`[install] venv_dir`, `--venv`) | short, user-owned, no sudo. `/usr/local/voiced` was rejected: it is root-owned on Apple Silicon Macs, so creating and upgrading it would need sudo, which the installer never runs. |
| config | `~/.config/voiced/config.toml` | copied from the packaged `config.example.toml` on first install, never overwritten |
| staged plists and scripts | `~/.config/voiced/launchd/` | `<label>.plist` x3, `install-daemons.sh`, `uninstall-daemons.sh`, `metal-probe.sh`; the sudo scripts reference these absolute paths |
| logs | `~/Library/Logs/voiced/` (`[server] log_dir`) | the macOS convention, visible in Console.app |
| models | Hugging Face cache (`~/.cache/huggingface` or `[install] hf_home`) | not vendored; `HF_HOME` is passed to the daemons when configured |
| installed plists | `/Library/LaunchDaemons/<label>.plist`, root:wheel 0644 | written only by the printed sudo script |

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

`install --dry-run` prints the plan, every command, all three plists and the
sudo script, and writes nothing. The real run writes the config (if absent),
the log dir, the venv and the staging directory, then prints the sudo script
and stops. The user runs `sudo bash ~/.config/voiced/launchd/install-daemons.sh`.

`uninstall` prints the matching bootout-and-remove script first and refuses
to delete anything while any label is still loaded (otherwise launchd would
respawn a missing binary every 10 s). Deleting needs `--yes`; models, logs
and config each need their own flag. Only two kinds of directory are deleted
recursively: the venv (only if it contains `pyvenv.cfg`) and the models'
Hugging Face cache entries. Everything else is deleted file by file, because
the directories may be shared: `log_dir = "~/Library/Logs"` and
`VOICED_CONFIG=~/config.toml` (staging dir `~/launchd`) are legal
configurations. `--remove-logs` removes the per-service log files, their
rotations and the Metal probe result; `--remove-config` removes the config
file, the staged plists and the generated scripts. A directory is removed
afterwards only if that left it empty. Every recursive delete is also
guarded against the filesystem root, the home directory and its ancestors,
and symlinks.

### Logs

Each service writes its log through a `RotatingFileHandler` to
`<log_dir>/<service>.log` (10 MB, five backups). The plist also points
`StandardErrorPath` at `<service>.stderr.log`, and launchd never rotates
that file, so a service that logged every line to stderr as well would grow
it without bound and duplicate the rotated log. `configure_logging`
therefore adds a stderr handler only when there is no log directory or
stderr is a terminal (`voiced serve` run by hand, which shows every line).
Under launchd stderr is a file, so no log records go there; the stderr file
keeps only what bypasses logging: a traceback from a crash during import or
before logging is configured, and errors raised by the logging system
itself. The watchdog follows the same rule (ADR 0012). `voiced logs` reads
the rotated `<service>.log`; `voiced logs --stderr` adds the stderr file
when looking for a crash.

`HF_HOME` is resolved once when the plan is built (`[install] hf_home`,
else the installing shell's `HF_HOME`, else unset) and written identically
into the plists and the model download command, so the daemons find the
weights the installer fetched.

Every path that ends up in a generated shell script is quoted with
`shlex.quote`, and `[launchd] label_prefix` is validated to letters, digits,
dots and dashes, because those scripts run as root. Re-running the install
script boots the old instance out and waits for launchd to finish tearing it
down before bootstrapping again; `bootout` is asynchronous and an immediate
`bootstrap` fails.

## Consequences

- Upgrading is `voiced install` again: the venv is reused
  (`uv venv --allow-existing`), the package upgraded, the plists re-staged, and
  the printed script boots the daemons out and back in.
- The venv path appears in the plists, so moving it means re-running
  `install` and the sudo script.
- `voiced doctor` reports the venv's Metal device using the venv's Python,
  not the interpreter running `doctor`.
