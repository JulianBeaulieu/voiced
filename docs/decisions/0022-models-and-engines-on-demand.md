# 0022: Models and engines on demand from the app

Date: 2026-10-20. Status: proposed.

## Context

An install (ADR 0018) builds one environment from `requirements.lock` with
every engine's Python packages, then downloads the pinned model of each
*configured* engine. After that the user cannot see what is on disk, cannot
fetch another model with progress, and cannot remove one. Switching engines
in Settings writes the config and restarts the service, which then downloads
the new model inside `load()`, silently; if that fails the service exits and
launchd restarts it every 10 s (the loop ADR 0013 guards against for an engine
without a model). The owner asked for the menu bar app to download only the
models wanted and to delete the ones no longer wanted.

Engine knowledge is spread over the engine classes (`model_id`, `license`,
`languages`, `download_patterns()`, `catalogue_voices()`), `voiced.models.PINS`
(revision and bytes for Parakeet and Kokoro; Soprano is unpinned and loads
`main` through `load_model(model_id)`) and the app (`engines.list` for its
pickers, `install-manifest.json` for the welcome screen). Phonon, being added
in parallel as ADR 0021, brings a CPU engine with a default model, gated 40m
models under a non-commercial licence, and a voices repository every model
needs (`gradium/phonon-voices`). More STT and TTS engines will follow. The
design must make the next ten cheap to add and keep ADR 0018's install,
update, rollback, repair and uninstall.

The implementation is a later change. Measurements were taken on 2026-10-20
on an M1 Max, macOS 15.7.4, uv 0.9.28: read-only against the real Hugging
Face cache, and in scratch environments and a scratch cache.

## Measured

**Packages.** An environment built from `requirements.lock` (`uv pip install
--require-hashes --no-deps`) holds 707 MB of installed files (summed from each
distribution's `RECORD`; `du` 726 MB). `uv export --frozen` lists 27, 74, 68
and 89 packages for no extra, `stt`, `tts` and both; three of them carry
markers that exclude macOS (`colorama`, `audioread`, `httpx2-jsfetch`), so 86
are installed, split by which extras pull them in:

| Set | Packages | Installed | Largest |
|---|---|---|---|
| core | 26 | 91 MB | imageio-ffmpeg 49, numpy 21 |
| shared by `stt` and `tts` | 25 | 259 MB | mlx-metal 163, scipy 71, hf-xet 8 |
| `stt` only (parakeet-mlx) | 20 | 183 MB | llvmlite 131, scikit-learn 31, numba 12 (librosa's) |
| `tts` only (mlx-audio, kokoro-onnx) | 15 | 174 MB | onnxruntime 78, transformers 50, espeakng-loader 20 |

**Candidate engines against the lock** (`uv pip install --dry-run` into that
environment with the lock's pins as constraints):

| Candidate | Result |
|---|---|
| `ptts>=0.4,<0.5` (Phonon) | resolves; adds only `ptts` 0.4.0, 16.7 MB installed |
| `torch` | resolves (2.14.1); adds 564 MB: torch 524, sympy 27, networkx 7, setuptools 3, mpmath 2 |
| `chatterbox-tts` | does not resolve: it pins `transformers==4.46.3` (up to 0.1.6) or `safetensors==0.5.3` (0.1.7); unconstrained it would downgrade huggingface-hub, numpy, scipy and transformers and install torch 2.6.0 |
| `voxcpm` | does not resolve: its `datasets` needs `fsspec<=2024.12.0` and an older `huggingface-hub` |

**The catalogue child** (`python -m voiced.engines.catalogue`, every engine
plugin imported, no weights): 1.66 s on the first run, then 0.24 to 0.25 s.

**The Hugging Face cache.** The lock has `huggingface_hub` 2.2.0, which keeps
a cache-wide shared blob store, `hub/blobs/<2 hex>/<xet hash>`, deduplicating
Xet files across repositories; a repository's `blobs/<etag>` is a relative
symlink into it. The development cache (18 GB, 17 repositories) also holds
other tools' repositories, some with detached revisions.

| Check | Result |
|---|---|
| `du -sk models--mlx-community--parakeet-tdt-0.6b-v3` | 404 KB: the 2,508,288,736-byte `model.safetensors` is a link into `hub/blobs/9a/` |
| `scan_cache_dir()` size of that revision | 2,509,044,141 bytes |
| `scan_cache_dir()` of the whole cache | 0.13 s, three runs |
| refs on the pinned revisions of Parakeet and Kokoro | `main`: early releases downloaded by branch |

The acceptance test, in a scratch `HF_HOME` with
`mlx-community/kitten-tts-nano-0.8` (61.7 MB): a `snapshot_download` by commit
SHA wrote no ref, and the repository directory held 52 KB, its two blobs
being links into the store. `delete_revisions(sha)` reported 61,731,543 bytes
to free; executed, the hub went from 60,320 KB to 8 KB, the store emptied and
the repository directory was removed (its only revision). Downloaded again by
branch, the same revision carried the ref `main`, and the same delete removed
it with the ref.

So today's `voiced uninstall --remove-models`, which `rmtree`s
`models--<repo>` (ADR 0006), frees about 404 KB of Parakeet's 2.5 GB here and
leaves the blob in the store with nothing pointing at it.

**Licences** from the model cards: Parakeet TDT 0.6B v3 CC-BY-4.0,
Kokoro-82M Apache-2.0; Phonon 31M Apache-2.0 and the 40M checkpoints
CC-BY-NC-4.0 (ADR 0021). Soprano's card is not cached; its pin records it.

## Decision

### 1. One catalogue, with a generated lock

Two files in `src/voiced/`, in the relation of `pyproject.toml` to `uv.lock`:

- `catalogue.py`, written by hand, standard library only:

  ```python
  CATALOGUE: tuple[Engine, ...] = (
      Engine(
          "phonon",
          models=(
              Model("gradium/phonon-31m-en", licence="Apache-2.0", patterns=PHONON),
              Model("gradium/phonon-40m-en", licence="CC-BY-NC-4.0", patterns=PHONON, gated=True),
          ),
          shared=(Model("gradium/phonon-voices", licence=VOICES_LICENCE, patterns=VOICES),),
      ),
  )
  ```

  `name` is the entry point name the config's `engine =` uses. The first
  model is the engine's default. `patterns` are the Hugging Face file
  patterns the engine loads (Phonon's are `config.json`, `tokenizer.json`,
  `model.q8.gguf` and `default-voice.safetensors`; the voices' patterns and
  licence are ADR 0021's). `shared` lists repositories every model of the
  engine needs, such as Phonon's voices: they are downloaded with any of its
  models, counted in its sizes, and protected
  while the engine is in use. `gated` marks a repository that needs a token
  and the user's acceptance of its terms on Hugging Face.
- `models.lock.json`, generated by `python -m voiced.models pin [<repo>]` (a
  maintainer's command, run when a model is added or moved): per repository
  the commit SHA, the patterns it was resolved with, each matching file with
  its size and the hash the hub reports (sha256 for LFS and Xet files), and
  `previous`, the revisions earlier releases pinned, which `pin` moves there.

Each fact has one home. Repository, licence, gated and patterns are in
`catalogue.py`; revision, files, bytes and previous revisions in the lock;
voices, languages and the code in the engine module. `bytes` is the sum of
the locked files, so it cannot drift from the patterns, and a test fails when
a catalogue entry's patterns differ from the ones its lock entry was resolved
with. What goes: `PINS` and Phonon's `OPTIONAL_PINS` (`voiced.models` reads
the lock), the engines' `download_patterns()` and Phonon's
`extra_downloads()` (the catalogue has both). `base.py` is frozen, so engines
keep the `model_id` and `license` attributes it requires, assigned from the
catalogue (`model_id = default_model("parakeet").repo`), and a test checks
every catalogued engine's two attributes against it. `label` stays
`engine_label()` and the kind stays the registry group. Adding an engine is
one catalogue entry, `pin`, its module and entry point, and its extra in the
lock (section 2).

`voiced models list --json` is the one view of it: per engine, its label,
kind, languages, voices with labels and whether its entry point is installed;
per model (shared ones included), the repository, revision, licence, `gated`,
download size, bytes on disk at that revision (`scan_cache_dir`), whether it
is downloaded (every locked file is in `snapshots/<revision>/`, checked by
`stat` alone), older locked revisions still cached with their sizes, refs on
the pinned revision, and `in_use`: which service's config names it. `in_use`
follows the config file, not the running process. The app always runs this
command (`~/.voiced/bin/voiced models list --json`, under half a second warm
by the measurements above); the control protocol does not change.
`engines.list` stays for the settings checks, built from the catalogue
joined with the registry; an engine from a third-party plugin the catalogue
does not know is listed as today, with its `model_id` and nothing to manage.
`install-manifest.json` keeps listing the default models only, for the
welcome screen.

### 2. Packages: every catalogued engine in the one lock

The environment keeps every catalogued engine's packages, from the one
`requirements.lock`; only models are added and removed. A new engine adds an
extra that `scripts/refresh_locks.py` includes, so it co-resolves in
`uv.lock`. Every catalogued engine is then importable, `lock_sha256` still
identifies the whole environment, and ADR 0018's update, rollback, repair and
uninstall need no change. Two costs are accepted: up to 183 MB of packages a
one-service user does not run, and a new engine still needs a voiced release.

Phonon fits (16.7 MB). A torch engine co-resolves but adds 564 MB, taking the
environment to about 1.27 GB; two of the survey's candidates, chatterbox and
VoxCPM, do not co-resolve at all. So this is revisited with the first engine
that needs torch or cannot co-resolve. Because every extra comes from one
`uv.lock`, per-engine hashed exports (`uv export --extra <engine>`) are
available then without re-resolving.

### 3. Storage, and what voiced may delete

Models stay in the Hugging Face cache at `[install] hf_home` (ADR 0006).
Other MLX tools share it, and the Xet store deduplicates across repositories;
a private cache would download and store again what the user has.

voiced deletes only a `(repository, revision)` pair its lock names, the pin
or one in `previous`, and only through `huggingface_hub`:
`scan_cache_dir(cache).delete_revisions(revision)`, which removes the
snapshot, the refs on it, the blobs no other cached revision uses, the
repository directory when that was its only revision, and the shared-store
blobs nothing references any more. It never deletes a repository outside the
catalogue, a revision it did not pin, or a path it computed itself.

A ref on a pinned revision means it was also fetched by branch, by another
tool or an earlier voiced (both pins here carry `main`); the delete takes the
ref too. voiced does not refuse, which would block deleting its own 2.5 GB,
but says so: "Also used as `main` by another download; it will be fetched
again when needed." Files another process has open or mapped stay readable
until it closes them, so a delete does not break a loaded model before its
next load.

Sizes come from the same scan: on disk is the revision's `size_on_disk`; a
delete confirms with the strategy's `expected_freed_size`, which leaves out
blobs another revision or repository keeps. `voiced uninstall
--remove-models` uses the same delete for every locked revision found.

### 4. The CLI

```
voiced models list [--json]
voiced models download <repo> [--json-progress]
voiced models delete <repo> [--previous] [--yes] [--json]
```

- `download` of a catalogue model fetches its locked files and its engine's
  `shared` repositories, with ADR 0018's byte progress. A repository the
  catalogue does not know is the advanced path: fetched whole at `main`,
  unpinned, with a notice, as `voiced install` does today for a config that
  names one. voiced never deletes such a repository.
- `delete` removes the pinned revision of a catalogue model; with
  `--previous`, only its older locked revisions. A `shared` repository is
  deleted with the last of its engine's models. It refuses the model in use
  (section 6) and confirms without `--yes`.
- Use has a CLI equivalent already: `voiced settings set tts.engine=phonon
  tts.model=gradium/phonon-40m-en`.
- `--json-progress` speaks ADR 0018's protocol 1 (`hello`, `download`, `log`,
  then `done` or `error`). An `error` carries a `code`: `token_invalid` (a
  401), `gated_terms` (a 403 on a gated repository, with its URL), `network`,
  `disk` (checked first: the remaining bytes plus 1 GB must be free), `busy`
  or `failed`; unknown codes are shown as text.
- `download` and `delete` take ADR 0017's install lock without waiting
  (`flock` with `LOCK_NB`); a held lock is `busy`. The lock records no holder,
  so the app says "voiced is installing or updating; try again when it ends".
  `voiced install`'s models step calls the download function in-process under
  the lock it already holds (`python -m voiced.models download --config`
  goes). SIGTERM ends a download as ADR 0018's Cancel does; the
  `.incomplete` files resume it.
- `voiced doctor` gains a line per service: the configured model is
  downloaded at its pin, or missing with the command that fetches it.

### 5. What the user sees

There is no new pane. The Speech to Text and Text to Speech panes replace
their engine picker with a list of models, one row per model, grouped under
its engine (Phonon shows 31m and 40m as two rows). The voice picker stays.

Each row: the model's name, the licence (linked to the model card), a size
labelled "on disk" or "download", the state and its actions:

| State | Actions |
|---|---|
| Not downloaded | Download, Use |
| Downloading | a byte bar with speed and time left; Cancel |
| Downloaded | Use, Delete |
| In use | "In use"; Delete disabled: "In use. Choose another model first." |
| Update available (an older locked revision is cached) | Update (download size), "Delete old version (2.1 GB)" |
| Gated | Download asks for a token first (below) |

- **Download** runs `~/.voiced/bin/voiced models download <repo>
  --json-progress` as the app's child, as the installer runs `voiced
  install`: it takes minutes, needs the token that lives in the user's
  session, and works while the services are down; the watchdog does no long
  work (ADR 0012). One at a time. While it runs the menu bar icon shows busy,
  the menu window has a progress row with Cancel, the download continues when
  Settings closes, quitting the app asks first, and Update and Repair wait.
- **Delete** confirms "Delete <model>? This frees <freed>. Downloading it
  again takes <size>." (with section 3's note when the revision has a ref),
  then runs `voiced models delete <repo> --yes --json`.
- **Use** downloads first if needed, then sends `settings.set` with the
  engine and the model together (`tts.engine` and `tts.model`), so Use on
  phonon-40m does not get the engine's default model from ADR 0013. The row
  shows restarting until `status` reports the service ready. After a TTS
  switch the app says: "In Home Assistant, pick a <engine> voice under
  Settings > Voice assistants."
- A footer: the total voiced uses, the cache location with Show in Finder,
  and "This folder is shared with other apps; voiced deletes only its own
  models."

**The Hugging Face token** is asked for when a gated model is downloaded, in
a sheet that says why ("<model> is gated: Hugging Face shares it only with
accounts that accept its terms") and orders the steps: accept the terms
(link), create a read-only token (link), paste it. The app checks it with
`whoami` before saving it, stores it in the login keychain (a generic
password, service `io.github.julianbeaulieu.voiced`, account
`huggingface-token`), and hands it to the download child alone, as
`HF_TOKEN` in that child's environment: never on a command line (visible in
`ps`), never in the config file, the services, the watchdog or the plists.
The sheet offers "Remove token". voiced never writes `$HF_HOME/token`, which
belongs to `hf auth login` and every other tool sharing the cache; without a
keychain token `huggingface_hub`'s own lookup applies, so a user who ran `hf
auth login` needs nothing new. This amends ADR 0018's permission table: the
keychain is used once a token is entered (an ad hoc build, a new code
identity each time, is asked again after each rebuild).

### 6. Switching engines, and a model that is not there

Downloading never switches; Use switches through `settings.set`, which
restarts the service (ADR 0013).

1. **The services never download.** They run with `HF_HUB_OFFLINE=1`, and
   every catalogued engine loads from the directory `snapshot()` returns
   (Soprano included, which gains a catalogue entry, a pin and patterns). A
   model reaches the disk only through `voiced models download`, `voiced
   install` or the user, with progress, never at boot.
2. **`load()` checks before loading.** `snapshot()` runs the same per-file
   check as `models list` against the lock and raises a new
   `ModelNotDownloaded` (repository, revision, the command) when a file is
   missing, so a partial snapshot is caught without the hub's tree listing.
3. **`apply_settings` refuses a model that is not downloaded** with a new
   error code, `model_not_downloaded`: "gradium/phonon-40m-en is not
   downloaded; run voiced models download gradium/phonon-40m-en, then voiced
   settings set tts.engine=phonon tts.model=gradium/phonon-40m-en". The check
   sits in `apply_settings`, so the control op and the status page's form
   (`POST /settings`, inside the service) both get it, and it reads the disk
   each time, not `engines.list`'s 60 s cache. A model outside the catalogue
   is not checked. `models delete` refuses the model in use the same way.
4. **A service whose model is missing anyway** (the config edited by hand,
   the cache cleared by another tool or `hf cache delete`) does not exit. The
   service maps `ModelNotDownloaded` and the hub's offline and not-found
   errors (`LocalEntryNotFoundError`, `RepositoryNotFoundError`,
   `RevisionNotFoundError`, `EntryNotFoundError`) from any engine's `load()`
   to a new health status, `missing_model`: `/health` answers 503 with
   `last_error` naming the repository and the command, requests get 503
   `model_not_downloaded`, a Wyoming client an `error` event. The watchdog's
   `Kind` gains `missing_model`: `poll_health` maps the status to it (today
   an unknown status is `error`, which `decide` restarts) and `decide`
   returns `none` for it, since a restart cannot fix it. launchd sees a
   running process. The menu bar icon shows its error badge and the menu
   says "Speech to Text: model not downloaded" with Download, which downloads
   and then sends `service.restart`. Any other load failure still exits, as
   ADR 0012 wants.
5. **An unknown voice falls back.** Home Assistant keeps sending the voice
   its pipeline was set up with (`af_heart`) after the TTS engine changes,
   and `check_voice` rejects a voice the engine does not serve, so the
   assistant would go silent. `check_voice` now serves `tts.voice` instead,
   or the engine's first voice if that is not served either, and logs a
   warning naming both. This changes ADR 0007's HTTP behaviour too: an
   OpenAI client's `alloy` gets the configured voice rather than a 400.

### 7. Install, update, rollback, repair, uninstall

Install downloads only the configured engines' default models, as today.
An update downloads the new pin of each *configured* model; another
downloaded model whose pin changed shows "Update available". An update
deletes nothing, so a rollback finds its pins still cached, and a rollback's
`voiced install` fetches them again if the user deleted them. Repair never
touches models. Uninstall keeps its models checkbox, through section 3's
delete.

## Alternatives considered

- **Per-engine package sets installed on demand** (a per-extra hashed export
  into the active environment). Feasible, but it ends "one lock hash
  identifies the environment": `install.json` would record the added extras,
  update and rollback would replay them, repair would reinstall each, and
  `hello`'s comparison would need the set; it also writes packages into an
  environment the services import from. It saves at most 183 MB today. Kept
  for the first torch engine (section 2).
- **A separate environment per engine.** The plists run
  `~/.voiced/bin/voiced`, one link to one environment (ADR 0018); per-engine
  environments need a link per service, another root step, and an update,
  rollback and repair each. It is also the only way to run chatterbox or
  VoxCPM beside today's lock, so it returns with them, not before.
- **A private model directory under `~/.voiced`.** Simple deletes, but it
  downloads again what other tools have and loses the Xet deduplication.
- **A `models.list` control op beside the CLI.** A second child process and
  op for the same data, a protocol change and a fake-server scenario; the app
  already runs the CLI for downloads and when the services are down.
- **A separate Models pane.** One more pane in a fixed 700 by 500 window,
  and two places that switch engines with different rules.
- **Downloads through the control socket.** A minutes-long child in the
  watchdog, the token in a daemon outside the user's login session, and
  progress over a channel built for short requests.

## Consequences

- Adding an engine needs no app change: the app renders `models list`.
- New names: `voiced models list|download|delete`, the error codes
  `model_not_downloaded`, `token_invalid` and `gated_terms`, the health status
  and watchdog kind `missing_model`, the exception `ModelNotDownloaded`, and
  `models.lock.json` as package data. No new control op; protocol 1 holds.

### First implementation, and later

First: `catalogue.py`, `models.lock.json` and `pin`; engines reading
`model_id` and `license` from it; `voiced models list|download|delete`;
install's models step in-process; offline loading, `ModelNotDownloaded`,
`missing_model` in the service and the watchdog; `model_not_downloaded` in
`apply_settings` and `models delete`; the voice fallback; the `doctor` line;
the model rows in the two panes; uninstall through the new delete. Gated
models are listed with a link to `hf auth login` and their terms; the token
sheet and keychain come when the first gated model is asked for.

Later, each when needed:

- A static mirror for Hugging Face outages and blocked networks, checked
  against the lock's per-file hashes, as Handy (ADR 0018) does: mirror URLs
  `{mirror}/{repo_id}/{revision}/{filename}` with a sha256 check per file in
  `src-tauri/src/catalog/mod.rs`, licences per model in `catalog.json` beside
  it. Gated models are never mirrored.
- Several downloads at once; voice previews.
- Pruning older locked revisions once no kept environment pins them.
- Per-engine packages or environments, with the first torch engine.
