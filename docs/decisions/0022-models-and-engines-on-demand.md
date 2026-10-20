# 0022: Models and engines on demand from the app

Date: 2026-10-20. Status: proposed.

## Context

An install today (ADR 0018) builds one environment from `requirements.lock`
with every engine's Python packages, then downloads the pinned model of each
*configured* engine. After that the user cannot see what is on disk, cannot
fetch another engine's model with progress, and cannot remove one. Switching
engines in Settings writes the config and restarts the service, which then
downloads the new model inside `load()`, silently, with no progress, and
crash-loops through launchd if the download fails (the case ADR 0013 already
guards for an engine without a model). The owner asked for the menu bar app
to download only the models wanted and to delete the ones no longer wanted.

Engine knowledge is spread over three places: the engine classes (`model_id`,
`languages`, `download_patterns()`, `catalogue_voices()`), `voiced.models.PINS`
(revision and byte count, for two of the three engines: Soprano is not
pinned and loads `main`), and the app, which reads `engines.list` over the
control channel for its pickers and `install-manifest.json` for the welcome
screen's sizes. A Phonon TTS engine (CPU, the 6.5 MB `ptts` wheel, a model of
about 93 MB, optional 40m models gated behind a Hugging Face token) is being
added in parallel, and more STT and TTS engines will follow. The design must
make the next ten engines cheap to add and must keep ADR 0018's install,
update, rollback, repair and uninstall stories intact.

This record is the design; the implementation is a later change. Everything
marked *measured* was run on the development machine (M1 Max, macOS 15)
on 2026-10-20, read-only against the real Hugging Face cache, and against a
scratch environment built from the shipped lock.

## Measured

**Packages per extra.** An environment built from `requirements.lock`
(`uv pip install --require-hashes --no-deps`) holds 707 MB of installed files
(by each distribution's `RECORD`; `du` 726 MB with the Python links). Split by
which extras pull each package in (`uv export --frozen --extra ...`):

| Set | Packages | Installed | Largest |
|---|---|---|---|
| core | 26 | 91 MB | imageio-ffmpeg 49, numpy 21 |
| shared by `stt` and `tts` | 25 | 259 MB | mlx-metal 163, scipy 71, hf-xet 8 |
| `stt` only (parakeet-mlx) | 20 | 183 MB | llvmlite 131, scikit-learn 31, numba 12 (librosa's) |
| `tts` only (mlx-audio, kokoro-onnx) | 15 | 174 MB | onnxruntime 78, transformers 50, espeakng-loader 20, tokenizers 10 |

Splitting packages per engine would save a TTS-only user at most 183 MB and an
STT-only user 174 MB. The default models are 2,509 MB (Parakeet) and 355 MB
(Kokoro); Soprano's revision on disk is 221 MB. Models are where the disk goes.

**The Hugging Face cache.** The lock installs `huggingface_hub` 2.2.0. This
Mac's cache (`~/.cache/huggingface/hub`, 18 GB, 17 repositories) is shared
with other tools: besides voiced's three there are VoxCPM2, chatterbox,
Marvis, pocket-tts and others, and pocket-tts holds three detached revisions
with no ref. `huggingface_hub` 2.x keeps a cache-wide shared blob store,
`hub/blobs/<2 hex>/<xet hash>`, deduplicating Xet files across repositories;
each repository's `blobs/<etag>` is then a relative symlink into it.

| Check | Result |
|---|---|
| `du -sh models--mlx-community--parakeet-tdt-0.6b-v3` | 404 KB: the 2,508,288,736-byte `model.safetensors` is a symlink to `hub/blobs/9a/...` |
| `scan_cache_dir()` size of that revision | 2,509,044,141 bytes (follows the links) |
| `delete_revisions(<Parakeet pin>).expected_freed_size` (strategy computed, not executed) | 2,509,044,141; Kokoro 389,410,807; Soprano 220,642,837 |
| `scan_cache_dir()` of the whole 18 GB cache | 0.13 s, three runs |
| Kokoro's pinned revision on disk | 122 files, 389 MB, where the pin's `download_patterns()` name 56 files, 355 MB: an earlier unpinned download also fetched the sample WAVs |

So `voiced uninstall --remove-models`, which `rmtree`s `models--<repo>` (ADR
0006), frees 404 KB of Parakeet's 2.5 GB on such a cache and leaves the blob in
the shared store with nothing pointing at it. `huggingface_hub`'s own
`DeleteCacheStrategy` records the shared targets before removing the
repository's links and sweeps those no other repository references.

**Licences**, from the cached model cards: Parakeet TDT 0.6B v3 is
CC-BY-4.0, Kokoro-82M Apache-2.0. Soprano's card is not in the cache (its
download skipped the README); its licence is recorded when it is pinned.

## Decision

### 1. One catalogue, in voiced

`voiced.models` replaces `PINS` with `CATALOGUE`, a typed table, standard
library only (the app's bundle script still reads it from the unpacked
wheel):

```python
Engine(name="parakeet", kind="stt", label="Parakeet", compute="gpu", models=(
    Model(repo="mlx-community/parakeet-tdt-0.6b-v3",
          revision="ed2b7e8c...", bytes=2508532829, licence="CC-BY-4.0",
          gated=False, previous=()),
)),
```

- `name` is the engine's entry point name, which the config's `engine =`
  uses. The first model is the engine's default (what `settings.set` gives a
  new engine without a model, ADR 0013).
- `revision` is a commit SHA; `bytes` is the size of the files the engine's
  `download_patterns()` names at that revision, as today.
- `licence` is an SPDX identifier from the model card, shown in the app,
  because a model's licence can differ from its engine's code (Handy lists
  CC-BY-NC models with their licence in the same way).
- `gated` marks a repository that needs a token and the user's acceptance of
  its terms on Hugging Face (Phonon's 40m models).
- `previous` lists the revisions earlier releases pinned. The script that
  re-pins (`python -m voiced.models pin <repo>`, run by a maintainer, which
  computes `bytes` with a dry-run `snapshot_download`) moves the old revision
  there. It is what lets voiced recognise its own revisions in a shared cache
  (section 3).

What stays in the engine module: `download_patterns()`, `languages`,
`catalogue_voices()` and the code. Adding an engine is one `CATALOGUE` entry,
its module, its entry point and its packages in the lock (section 2).

`engines.list` (ADR 0013) is built from the catalogue joined with the
registry: a catalogue engine whose entry point is missing in this environment
is left out, and an engine from a third-party plugin that the catalogue does
not know is listed as today, with its `model_id` and no size, licence or
download state. voiced stays pluggable; the app manages only what the
catalogue names.

One function, `models_view(settings)`, gives every view of the catalogue: per
engine its label, kind, compute, languages, voices with labels, whether its
entry point is installed, and per model the repository, pinned revision,
licence, `gated`, the pin's download size, the bytes of that revision on disk
(`scan_cache_dir`, 0 when absent), whether it is downloaded (the pinned
revision is cached with every file the patterns name), older pinned revisions
still cached with their sizes, and which service uses it (`in_use`: the
config's `stt.model` or `tts.model`). The app reads it two ways:

- connected: a new control op, `models.list`, which the watchdog serves like
  `engines.list`, from a child process (`python -m voiced.models list`) under
  the same 8 s timeout, so no engine or `huggingface_hub` code runs in the
  watchdog. It is not cached, since downloads and deletes change it; the scan
  costs 0.13 s;
- not connected (services down, repair): the app runs
  `~/.voiced/bin/voiced models list --json` itself. Same function, same JSON.

`install-manifest.json` keeps listing only the default models: the welcome
screen sizes the first install, which downloads only those.

### 2. Packages: every catalogued engine in the one locked environment

The environment keeps every catalogued engine's packages, from the one
`requirements.lock`, and only models are installed and removed on demand. Each
new engine adds an extra to `pyproject.toml` that `scripts/refresh_locks.py`
includes, so it co-resolves with the rest in `uv.lock`. A catalogued engine is
therefore always importable, `install.json`'s `lock_sha256` still identifies
the whole environment, and ADR 0018's update (new environment from the
bundled lock), rollback (the previous environment as it was), repair
(reinstall the lock) and uninstall need no change. The cost is measured
above: a user who uses one service carries up to 183 MB of packages they do
not run, next to the gigabytes of model they chose.

This is revisited when a candidate engine cannot co-resolve with the lock (a
second `mlx` or a `torch` pin, say) or when the environment would grow past
1.5 GB (about twice today's). Because every extra already comes from one
`uv.lock`, per-engine hashed exports (`uv export --extra <engine>`) remain
possible then without re-resolving anything.

### 3. Model storage, and what voiced may delete

Models stay in the Hugging Face cache, at `[install] hf_home` as today
(ADR 0006). Other MLX tools on the same Mac share that cache, the Xet store
deduplicates across repositories, and a private voiced cache would download
and store again what the user already has.

voiced deletes only a `(repository, revision)` pair that the catalogue names,
the current pin or one in `previous`, and only through `huggingface_hub`:
`scan_cache_dir(cache).delete_revisions(revision)`, whose strategy removes the
snapshot, the refs pointing at it and the blobs no other cached revision uses,
then sweeps shared-store blobs no repository references any more. It never
deletes a revision it did not pin, a repository outside the catalogue, a
`models--*` directory as a whole, or a path it computed itself. Another
tool's revision of the same repository stays, and so do its blobs.

Sizes come from the same scan: "on disk" is the revision's
`size_on_disk`, and a delete confirms with the strategy's
`expected_freed_size`, which excludes blobs another revision or repository
keeps. Downloads use `snapshot_download` with the pinned revision and the
engine's patterns, with the byte progress ADR 0018 built.

`voiced uninstall --remove-models` uses the same delete for every catalogue
pin and `previous` revision found in the cache, instead of `rmtree`.

### 4. The CLI

```
voiced models list [--json]
voiced models download <engine | repo> [--json-progress]
voiced models delete <repo> [--previous] [--yes] [--json]
```

- `download kokoro` fetches the engine's default model, `download <repo>` a
  catalogue model by repository. A repository the catalogue does not know is
  refused; a config that names one is the advanced path, fetched as today by
  `hf download` or by `voiced install`'s models step (`main`, no pin).
- `delete` removes the pinned revision; with `--previous`, only the older
  pinned revisions still cached. It refuses the model in use (section 6)
  and asks for confirmation without `--yes`, showing what it frees.
- `--json-progress` speaks ADR 0018's protocol 1: `hello`, `download` events
  with bytes and total, `log`, then `done` or `error`. An `error` carries a
  `code`: `gated` (with the model page's URL, for a 401 or 403 on a gated
  repository), `network`, `disk` (checked before starting: the remaining
  bytes plus 1 GB must be free), `busy` or `failed`. Unknown codes are shown
  as text, as ADR 0013 does for errors.
- `download` and `delete` take ADR 0017's install lock without waiting, so
  a delete cannot race `voiced install`'s models step, an update or another
  download; a held lock is `busy`, naming the holder's command. SIGTERM ends a
  download as ADR 0018's Cancel does; the `.incomplete` files resume it.
- `voiced install`'s models step becomes `voiced models download` for each
  configured model (`python -m voiced.models download --config` goes).
- `voiced doctor` gains a line per service: the configured model is
  downloaded at its pinned revision, or missing with the command that fetches
  it, or gated without a token.

### 5. What the user sees: a Models pane

Settings gains a **Models** pane. It lists every catalogue engine in two
groups, Speech to Text and Text to Speech. Each row shows the engine's label,
its model, CPU or GPU, the languages, the licence (linked to the model card),
the size (on disk when downloaded, else the download size), the state, and
"In use" on the one each service runs. A TTS row expands to its voices.

| State | Actions |
|---|---|
| Not downloaded | Download |
| Downloading | a byte bar with speed and time left, as the installer's; Cancel |
| Downloaded | Use (for the other engines of that kind), Delete |
| In use | none; Delete is disabled with "In use by Speech to Text. Choose another engine first." |
| Older version cached | "Older version, 2.1 GB" with Delete; the current pin shows as Not downloaded |
| Gated | Download asks for a token first (below); a refused token says "Accept the model's terms at <link>" |
| Not in this voiced | the catalogue knows it but this environment lacks its entry point; "Update voiced" |

- **Download** runs `~/.voiced/bin/voiced models download <repo>
  --json-progress` as a child of the app, as the installer runs `voiced
  install`, not through the control socket: a download takes minutes, needs
  the token, which lives in the user's session (below), and works while the
  services are down; the watchdog stays free of long work (ADR 0012). One
  download at a time; the pane disables the others while one runs.
- **Delete** shows "Delete <model>? This frees <freed>. Downloading it again
  takes <size>." and runs `voiced models delete <repo> --yes --json`.
- **Use** sets the engine through `settings.set` (`stt.engine` or
  `tts.engine`; ADR 0013 then picks the engine's default model and a voice
  and restarts the service). The pane shows the service as restarting until
  `status` reports it ready. A "Use" on a model that is not downloaded
  downloads it first, then sets it.
- The engine pickers in the Speech to Text and Text to Speech panes stay; an
  engine whose model is not downloaded is shown with "(not downloaded)", and
  choosing it gets section 6's refusal, which the app turns into "Download it
  in Models" with a button to that row.

**The Hugging Face token** is asked for only when a gated model is
downloaded, in a sheet with a secure text field, a link to create a
read-only token and the model's terms page. The app stores it in the login
keychain (a generic password, service `io.github.julianbeaulieu.voiced`,
account `huggingface-token`) and hands it to the download child alone, as
`HF_TOKEN` in that child's environment: never on a command line (visible in
`ps` to every user), never in the config file, never in the services, the
watchdog or the plists. The models pane offers "Remove token". voiced does
not write `$HF_HOME/token`: that file belongs to `hf auth login` and every
other tool sharing the cache. Without a keychain token, `huggingface_hub`'s
own lookup applies, so a CLI user who ran `hf auth login` or exports
`HF_TOKEN` needs nothing new, and the app's children find that file too
(under the configured `HF_HOME`). This amends ADR 0018's permission table:
the keychain is used, only once a token is entered. A build signed ad hoc
gets a new code identity each time, so the keychain asks again after every
development rebuild; signed releases do not.

### 6. Switching engines, and a model that is not there

Downloading never switches; Use switches, through `settings.set`, which
restarts the service (ADR 0013). Three rules keep a missing model from
crash-looping the service:

1. **The services never download.** They load with `local_files_only`
   (`HF_HUB_OFFLINE=1` in the service process), so a model appears on disk
   only through `voiced models download`, `voiced install` or the user, with
   progress, never as a silent multi-gigabyte download at boot.
2. **`settings.set` refuses a model that is not downloaded**, with a new
   error code `model_not_downloaded` and the repository in the message
   ("mlx-community/Soprano-80M-bf16 is not downloaded; voiced models download
   soprano"). The check runs in the catalogue child, as the engine check
   does. A model outside the catalogue is not checked (advanced path).
   `models delete` refuses the model in use the same way, so the app cannot
   pull the floor from under the running service.
3. **A service whose model is missing anyway** (the config edited by hand,
   the cache cleared by another tool or `hf cache delete`) does not exit.
   `load()` raises `ModelNotDownloaded`; the service stays up and `/health`
   answers 503 with a new status, `missing_model`, and `last_error` naming the
   repository and the command. Requests get 503 `model_not_downloaded`, and a
   Wyoming client an `error` event. The watchdog reports the state in
   `status` and does not restart a service in it, since a restart cannot fix
   it; launchd sees a running process and does not respawn it. The menu shows
   "Speech to Text: model not downloaded" with "Download", which runs the
   download and then `service.restart`. Any other load failure still exits,
   as ADR 0012 wants.

### 7. Install, update, rollback, repair, uninstall

- **Install** is unchanged: the configured engines' default models, with
  progress. Nothing else is downloaded on first run.
- **Update** downloads the new pin of each *configured* model, as today.
  Another downloaded model whose pin changed shows "Older version" in the
  pane; it is fetched when the user downloads or uses it. Nothing is deleted
  by an update, so a rollback's environment finds its own pins still cached.
- **Rollback** runs the previous environment's `voiced install`, which
  fetches its configured pins if the user deleted them since.
- **Repair** is unchanged; it never touches models.
- **Uninstall** keeps the "models" checkbox, now through section 3's delete,
  sized by the scan.

## Alternatives considered

- **Per-engine package sets installed on demand** (`uv pip install
  --require-hashes` of a per-extra export into the active environment).
  Feasible, since the exports come from one `uv.lock`, but it ends "one lock
  hash identifies the environment": `install.json` would record the added
  extras, update and rollback would replay them into the new or previous
  environment, repair would reinstall the lock plus each, and `hello`'s lock
  comparison would need the set. It also writes packages into an environment
  the services are importing from. All that saves at most 183 MB today.
  Kept as the path for an engine that cannot co-resolve (section 2).
- **A separate environment per engine.** The plists run
  `~/.voiced/bin/voiced` (ADR 0018), one link to one environment; per-engine
  environments mean a link per service, a different root step, and an update,
  rollback and repair per environment. Rejected for the same saving.
- **A private model directory under `~/.voiced`.** Simple deletes (all of
  it is voiced's), but it downloads again what other tools already have,
  loses the Xet store's deduplication, and breaks `[install] hf_home` users.
- **Deleting by `rmtree` of `models--<repo>`** (today's uninstall). Frees
  404 KB of 2.5 GB on a cache with the shared store, leaks the blob, and
  removes other tools' revisions. Rejected; replaced by section 3.
- **Downloads through the control socket.** The watchdog would run a
  minutes-long child, need the token in a daemon outside the user's login
  session, where the login keychain is not reliably unlocked, and stream
  progress over a channel built for short requests. The app already runs the
  CLI as a child for install; the same pattern serves downloads.
- **The services downloading on load, as today.** No progress, a surprise
  2.5 GB at boot after a delete, and a crash loop when offline.
- **The token in the config file or `$HF_HOME/token`.** The config is read
  by every service and shown by `settings.get`; the token file is shared with
  other tools, and voiced writing it would change their identity too.
- **A model picker on first run.** ADR 0018 rejected it; the defaults work
  and the pane is where choice happens.

## Consequences

- Adding an engine: one `CATALOGUE` entry (pinned with the script), the
  engine module with `download_patterns()`, its entry point, and its extra in
  the lock. The app needs no change: it renders what `models.list` returns.
- The app gains a Models pane, a token sheet, a keychain item and one new
  control op; the protocol stays at 1 (new op, new fields, new error codes,
  new health status; old clients ignore or show them as text).
- `HealthStatus` gains `missing_model`; the watchdog and `voiced status`
  learn not to restart it. A model can no longer arrive silently at boot.
- `PINS` goes; `pinned_revision()` and `snapshot()` read the catalogue.
  Soprano gets a pin, a byte count and a licence.
- Old pinned revisions accumulate until the user deletes them in the pane;
  an update never deletes.
- The shared-blob leak of today's `uninstall --remove-models` is fixed by the
  same delete; until this lands it stays as described in Measured.

### First implementation, and later

First: the catalogue with `previous`, licences and `gated` (and the pin
script); `voiced models list|download|delete` with `--json` and
`--json-progress`; install's models step through it; offline loading,
`missing_model` and the watchdog rule; `model_not_downloaded` in
`settings.set` and `models delete`; the `doctor` line; `models.list`; the
Models pane with download, delete, Use and the token sheet; uninstall
through the new delete.

Later, each when needed:

- Per-file sha256 in the catalogue and a static mirror as a fallback for
  Hugging Face outages and blocked networks, the shape Handy uses (a mirror
  URL of `{mirror}/{repo}/{revision}/{file}`, populated from the catalogue,
  every file hash-checked). Gated models stay token-only, never mirrored.
- Several downloads at once; voice previews in the pane.
- Pruning older pinned revisions automatically once no kept environment
  pins them.
- Per-engine package sets, under section 2's conditions.
- Managing models of third-party plugin engines the catalogue does not know.
