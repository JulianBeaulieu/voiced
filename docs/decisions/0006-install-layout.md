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
  fails. Observed during the prototype benchmarks (reference notes: "espeak
  breaks when the venv path is long; keep the venv at a short path").
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
to delete the venv while any label is still loaded (otherwise launchd would
respawn a missing binary every 10 s), unless `--force`. Deleting anything
needs `--yes`; models, logs and config each need their own flag.

## Consequences

- Upgrading is `voiced install` again: the venv is reused
  (`uv venv --allow-existing`), the package upgraded, the plists re-staged, and
  the printed script boots the daemons out and back in.
- The venv path appears in the plists, so moving it means re-running
  `install` and the sudo script.
- `voiced doctor` reports the venv's Metal device using the venv's Python,
  not the interpreter running `doctor`.
