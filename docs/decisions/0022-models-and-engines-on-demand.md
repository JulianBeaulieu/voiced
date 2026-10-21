# 0022: Models and engines on demand from the app

Date: 2026-10-20. Status: accepted; the CLI side and the app side have both landed (implementation notes at the end).

## Context

An install (ADR 0018) builds one environment from `requirements.lock` with
every engine's packages and downloads the pinned model of each *configured*
engine. Then the user cannot see what is on disk, fetch another model with
progress, or remove one. Switching engines restarts the service, which
downloads the new model inside `load()`, silently; if that fails it exits and
launchd restarts it every 10 s (the loop ADR 0013 guards against). The owner
asked for the menu bar app to download only the models wanted and to delete
the ones no longer wanted.

Engine knowledge is spread over the engine classes (`model_id`, `license`,
`languages`, `download_patterns()`, `catalogue_voices()`), `voiced.models.PINS`
(Parakeet and Kokoro; Soprano loads `main` through `load_model(model_id)`)
and the app (`engines.list`, `install-manifest.json`). Phonon (ADR 0021, in
parallel) adds a CPU engine, gated 40m models under a non-commercial licence,
and a voices repository every model needs (`gradium/phonon-voices`). More
engines will follow; the design must make the next ten cheap and keep ADR
0018's install, update, rollback, repair and uninstall. The implementation
is a later change.

## Measured

On 2026-10-20, M1 Max, macOS 15.7.4, uv 0.9.28; read-only against the real
Hugging Face cache, otherwise in scratch environments and a scratch cache.

**Packages.** An environment built from `requirements.lock` (`uv pip install
--require-hashes --no-deps`) holds 707 MB of installed files (summed from each
`RECORD`; `du` 726 MB). `uv export --frozen` lists 27, 74, 68 and 89 packages
for no extra, `stt`, `tts` and both; three carry markers that exclude macOS
(`colorama`, `audioread`, `httpx2-jsfetch`), so 86 are installed:

| Set | Packages | Installed | Largest |
|---|---|---|---|
| core | 26 | 91 MB | imageio-ffmpeg 49, numpy 21 |
| shared by `stt` and `tts` | 25 | 259 MB | mlx-metal 163, scipy 71, hf-xet 8 |
| `stt` only (parakeet-mlx) | 20 | 183 MB | llvmlite 131, scikit-learn 31, numba 12 (librosa's) |
| `tts` only (mlx-audio, kokoro-onnx) | 15 | 174 MB | onnxruntime 78, transformers 50, espeakng-loader 20 |

**Candidate engines against the lock** (`uv pip install --dry-run` into that
environment, the lock's pins as constraints):

| Candidate | Result |
|---|---|
| `ptts>=0.4,<0.5` (Phonon) | resolves; adds only `ptts` 0.4.0, 16.7 MB |
| `torch` | resolves (2.14.1); adds 564 MB: torch 524, sympy 27, networkx 7, setuptools 3, mpmath 2 |
| `chatterbox-tts` | does not resolve: pins `transformers==4.46.3` (up to 0.1.6) or `safetensors==0.5.3` (0.1.7); unconstrained it downgrades huggingface-hub, numpy, scipy, transformers and installs torch 2.6.0 |
| `voxcpm` | does not resolve: its `datasets` needs `fsspec<=2024.12.0` and an older `huggingface-hub` |

**The catalogue child** (`<env>/bin/python -P -m voiced.engines.catalogue`,
Python 3.12.15, engine plugins imported, no weights): 1.66 s on the first run
after installing (no `.pyc` yet), then 0.24 to 0.25 s.

**The Hugging Face cache.** The lock has `huggingface_hub` 2.2.0, which keeps
a cache-wide shared blob store, `hub/blobs/<2 hex>/<xet hash>`, deduplicating
Xet files across repositories; a repository's `blobs/<etag>` is a symlink into
it. The development cache (18 GB, 17 repositories) also holds other tools'
repositories, some with detached revisions.

| Check | Result |
|---|---|
| `du -sk models--mlx-community--parakeet-tdt-0.6b-v3` | 404 KB: the 2,508,288,736-byte `model.safetensors` is a link into `hub/blobs/9a/` |
| `scan_cache_dir()`: that revision; the whole cache | 2,509,044,141 bytes; 0.13 s, three runs |
| refs on the pinned revisions of Parakeet and Kokoro | `main`: early releases downloaded by branch |

The acceptance test, in a scratch `HF_HOME` with
`mlx-community/kitten-tts-nano-0.8` (61.7 MB): a download by commit SHA wrote
no ref; `delete_revisions(sha)` reported 61,731,543 bytes to free and,
executed, took the hub from 60,320 KB to 8 KB, emptied the store and removed
the repository directory. Downloaded by branch, the revision carried `main`,
and the same delete removed it with the ref. Today's `voiced uninstall
--remove-models` instead `rmtree`s `models--<repo>` (ADR 0006): 404 KB of
Parakeet's 2.5 GB freed, the blob left in the store with nothing pointing at it.

**Licences** (model cards): Parakeet TDT 0.6B v3 CC-BY-4.0, Kokoro-82M
Apache-2.0, Phonon 31M Apache-2.0, 40M CC-BY-NC-4.0; Soprano's, at its pin.

## Decision

### 1. One catalogue, with a generated lock

Two parts, in the relation of `pyproject.toml` to `uv.lock`:

- `CATALOGUE` in `voiced/models.py` (now `voiced/models/catalogue.py`),
  written by hand, standard library only
  (where `PINS` is today; `voiced.engines.catalogue` keeps building
  `engines.list`):

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

  `name` is the entry point name the config's `engine =` uses; the first
  model is the default. `patterns` are the files the engine loads (Phonon's:
  `config.json`, `tokenizer.json`, `model.q8.gguf`,
  `default-voice.safetensors`; the voices' patterns and licence are ADR
  0021's). `shared` lists repositories every model of the engine needs: they
  are downloaded with any of its models, counted in its sizes, protected
  while the engine is in use, and deleted with its last model. `gated` marks
  a repository that needs a token and acceptance of its terms.
- `models.lock.json`, package data that `python -m voiced.models pin [<repo>]`
  updates (a maintainer's command): per repository the commit SHA, the
  patterns it was resolved with, each file with its size and sha256 (the hub
  reports it for LFS and Xet files; `pin` hashes small files, which carry
  only a git sha1), and `previous`. `pin` updates rather than rewrites: a
  replaced revision is added to `previous`, which accumulates.

Each fact has one home: repository, licence, gated and patterns in
`CATALOGUE`; revision, files, bytes and previous revisions in the lock;
voices, languages and code in the engine module. `bytes` is the sum of the
locked files. One test asserts that the catalogue and the lock name the same
repositories with the same patterns and fails with "run python -m
voiced.models pin <repo>", like `scripts/refresh_locks.py`'s check;
CONTRIBUTING gains an "Adding or moving a model" step with the
implementation. What goes: `PINS`, Phonon's `OPTIONAL_PINS`, the engines'
`download_patterns()` and Phonon's `extra_downloads()`.

`base.py` is frozen, so engines keep the `model_id` and `license` class
attributes it requires, assigned from the catalogue's default model
(`model_id = default_model("parakeet").repo`) and checked by a test. The
licence is per model, though: where the service constructs the engine with
the configured model (`stt/app.py`, `tts/app.py`) it sets the instance's
`license` from that model's entry, so `/health`, the status page and
Wyoming's `Describe` (`wyoming/info.py`) say CC-BY-NC-4.0 for phonon-40m.
`label` stays `engine_label()`, the kind the registry group.

`voiced models list --json` is the one view: per engine its label, kind,
languages, voices and whether its entry point is installed; per model
(shared ones included) the repository, revision, licence, `gated`, download
size, bytes on disk (`scan_cache_dir`), whether it is downloaded (every
locked file in `snapshots/<revision>/`, checked by `stat`), older locked
revisions cached with their sizes, refs on the pin, and `in_use`, which
follows the config file, not the running process. The app always runs this
command; the control protocol gains no op. `engines.list` stays for the
settings checks, built from the catalogue joined with the registry; a
third-party engine the catalogue does not know is listed as today, with
nothing to manage. `install-manifest.json` keeps the default models only.

### 2. Packages: every catalogued engine in the one lock

The environment keeps every catalogued engine's packages from the one
`requirements.lock`; only models come and go. A new engine adds an extra that
`scripts/refresh_locks.py` includes, so it co-resolves in `uv.lock`;
`lock_sha256` still identifies the environment, and ADR 0018's update,
rollback, repair and uninstall do not change. Accepted costs: up to 183 MB of
packages a one-service user does not run, and a new engine needs a release.

Phonon fits (16.7 MB). A torch engine co-resolves but adds 564 MB (about
1.27 GB in all); chatterbox and VoxCPM do not co-resolve. So this is
revisited with the first engine that needs torch or cannot co-resolve; then
per-engine hashed exports (`uv export --extra <engine>`) need no re-resolve.

### 3. Storage, and what voiced may delete

Models stay in the Hugging Face cache at `[install] hf_home` (ADR 0006): other
MLX tools share it and the Xet store deduplicates; a private cache would not.

voiced deletes only a `(repository, revision)` pair its lock names, the pin or
one in `previous`, and only through `huggingface_hub`:
`scan_cache_dir(cache).delete_revisions(revision)` removes the snapshot, its
refs, the blobs no other revision uses, the repository directory when that was
its only revision, and shared-store blobs nothing references. voiced never
deletes a repository outside the catalogue, a revision it did not pin, or a
path it computed itself.

A ref on a pinned revision means it was also fetched by branch, by another
tool or an earlier voiced (both pins here carry `main`), and the delete takes
it. voiced does not refuse, which would block deleting its own 2.5 GB, but
says: "This model is also cached as `main`, perhaps by another app; that app
downloads it again if it needs it." Open or mapped files stay readable until
closed, so a delete does not break a loaded model before its next load.

On disk is the revision's `size_on_disk`; a delete confirms with the
strategy's `expected_freed_size`, which leaves out blobs others keep. `voiced
uninstall --remove-models` uses the same delete for every locked revision.

### 4. The CLI

```
voiced models list [--json]
voiced models download <repo> [--json-progress]
voiced models delete <repo> [--previous] [--yes] [--json]
```

- `download` fetches a catalogue model's locked files and its engine's
  `shared` repositories, with ADR 0018's byte progress. A repository the
  catalogue does not know is the advanced path: fetched whole at `main`,
  unpinned, with a notice, as `voiced install` does today; voiced never
  deletes it.
- `delete` removes a catalogue model's pin; with `--previous`, only its older
  locked revisions. It refuses the model in use (section 6) and confirms
  without `--yes`.
- Use's CLI equivalent exists: `voiced settings set tts.engine=phonon
  tts.model=gradium/phonon-40m-en`.
- `--json-progress` speaks ADR 0018's protocol 1. Its `error` event gains a
  `code`, which the app must decode (`ProgressProtocol.swift` reads only
  `step` and `message` today): `token_missing` (a 401 with no token),
  `token_invalid` (a 401 with one), `gated_terms` (a 403 on a gated
  repository, with its URL), `network`, `disk` (checked first: the remaining
  bytes plus 1 GB must be free), `busy` or `failed`; unknown codes show as
  text.
- `download` and `delete` take ADR 0017's install lock without waiting
  (`LOCK_NB`); a held lock is `busy`, and since the lock records no holder the
  app says "voiced is installing or updating; try again when it ends". Only
  this command takes the lock: `voiced install`'s models step keeps running
  `python -m voiced.models download --config` in the new environment
  (`install.py`), so the download and each engine's test load use that
  environment's code; that internal entry point never takes the lock its
  parent holds. SIGTERM ends a download as ADR 0018's Cancel does; the
  `.incomplete` files resume it.
- `voiced doctor` gains a line per service: the configured model downloaded
  at its pin, or missing with the command that fetches it.

### 5. What the user sees

No new pane. The Speech to Text and Text to Speech panes replace their engine
picker with one row per model, grouped by engine (Phonon's 31m and 40m are two
rows); the list scrolls in the 700 by 500 window under a pinned footer. The
voice picker stays: it lists the in-use engine's voices and changes with Use.
Each row shows the name, the licence (linked to the model card), a size
labelled "on disk" or "download", the state and its actions:

| State | Actions |
|---|---|
| Not downloaded | Download, Use |
| Downloading | a byte bar with speed and time left; Cancel |
| Downloaded | Use, Delete |
| In use | "In use"; Delete disabled: "In use. Choose another model first." |
| Update available (pin missing, an older locked revision cached) | Update (download size), Use |
| Gated, not downloaded | Download, which asks for a token (below) |

Separately from its state, every row with an older locked revision cached,
the one in use included, offers "Delete old version (2.1 GB)".

- **Download** runs `~/.voiced/bin/voiced models download <repo>
  --json-progress` as the app's child, as the installer runs `voiced
  install`: it takes minutes, needs the token from the user's session, and
  works while the services are down; the watchdog does no long work (ADR
  0012). One at a time. Meanwhile the icon shows busy, the menu window has a
  progress row with Cancel, closing Settings does not stop it, quitting asks
  first, and Update and Repair wait.
- **Delete** confirms "Delete <model>? This frees <freed>. Downloading it
  again takes <size>." (plus section 3's note for a ref, and "Phonon's voices
  are deleted with it." for an engine's last model), then runs `voiced models
  delete <repo> --yes --json`.
- **Use** downloads first if needed, then sends `settings.set` with engine
  and model together, so Use on phonon-40m does not get ADR 0013's default
  model. The row shows restarting until `status` says ready. After a TTS
  switch: "In Home Assistant, pick a <engine> voice under Settings > Voice
  assistants > your assistant > Text-to-speech > Voice. If the new voices
  are missing, reload voiced under Devices & services." (Current Home
  Assistant rebuilds the list from polled info, `_handle_info_update`.)
- The footer: the total voiced uses, the cache location with Show in Finder,
  and "This folder is shared with other apps; voiced deletes only its own
  models."
- Error texts per `code`: `token_missing` and `token_invalid` open the token
  sheet, the latter saying "Hugging Face rejected this token"; `gated_terms`
  says "Accept the terms for <model> on Hugging Face" with the link;
  `network`, `disk` and `busy` say what to do; others show the message.

**The Hugging Face token.** A sheet says why ("<model> is gated: Hugging Face
shares it only with accounts that accept its terms") and orders the steps:
accept the terms (link), create a read-only token (link), paste it. The app
checks it with `whoami`, stores it in the login keychain (generic password,
service `io.github.julianbeaulieu.voiced`, account `huggingface-token`), and
hands it to the download child alone, as `HF_TOKEN` in its environment: never
on a command line (visible in `ps`), the config file, the services, the
watchdog or the plists. The sheet offers "Remove token". voiced never writes
`$HF_HOME/token`, which belongs to `hf auth login` and the other tools; without
a keychain token `huggingface_hub`'s own lookup applies, so `hf auth login`
users need nothing new. This amends ADR 0018's permission table: the keychain
is used once a token is entered (an ad hoc build is asked again after each
rebuild).

### 6. Switching engines, and a model that is not there

Downloading never switches; Use does, through `settings.set` (ADR 0013).

1. **The services never download.** They run with `HF_HUB_OFFLINE=1`, and
   every catalogued engine loads from the directory `snapshot()` returns.
   Soprano gains a catalogue entry, a pin and patterns; Kokoro, which hands
   mlx-audio the repository id after `snapshot()`, finds the pinned revision
   offline because it is cached. A model reaches the disk only through
   `voiced models download`, `voiced install` or the user, never at boot.
2. **`load()` checks first.** `snapshot()`, also the download function, gains
   a check-only mode: the same per-file check as `models list`, raising a new
   `ModelNotDownloaded` (repository, revision, the command) when a file is
   missing, so a partial snapshot is caught without the hub's tree listing.
3. **`apply_settings` refuses a model that is not downloaded** with a new
   code, `model_not_downloaded`: "gradium/phonon-40m-en is not downloaded;
   run voiced models download gradium/phonon-40m-en, then voiced settings set
   tts.engine=phonon tts.model=gradium/phonon-40m-en". In `apply_settings`,
   the control op and the status page's form (`POST /settings`, in the
   service) both get it. `_ENGINE_KEYS` gains `stt.model` and `tts.model`;
   the check runs even when `engines` is None, reads the disk each time (not
   `engines.list`'s 60 s cache) and raises a new `ModelMissing`, which both
   callers map (today they map only `InvalidSetting`). A model outside the
   catalogue is not checked. `models delete` refuses the model in use alike.
4. **A service whose model is missing anyway** (a hand-edited config, a cache
   cleared by another tool) does not exit. It maps `ModelNotDownloaded` and
   the hub's offline and not-found errors (`LocalEntryNotFoundError`,
   `RepositoryNotFoundError`, `RevisionNotFoundError`, `EntryNotFoundError`)
   from any engine's `load()` to a new health status, `missing_model`;
   `/health` answers 503 with `last_error` naming the repository. The hooks:
   - `HealthStatus` (`health.py`) gains `missing_model`, and so does protocol
     1's service state: an added value, which the app's decoder maps to
     `.unknown` until it knows it;
   - `check_loaded` (`context.py`) raises `model_not_downloaded`, a 503
     without `Retry-After`, not the busy `EngineNotLoaded`; Wyoming clients
     get an `error` event;
   - the watchdog's `Kind` gains `missing_model`: `poll_health` maps the
     status to it (an unknown status is `error` today, which `decide`
     restarts) and `decide` returns `none`;
   - `supervisor.service_state` maps that kind to the state `missing_model`
     (it would read as starting or degraded).

   launchd sees a running process. `public_health` strips `last_error` when
   an API key is set, so the app takes the repository from the status's
   `model` field. The icon shows its error badge and the menu says "Speech to
   Text: model not downloaded" with Download, which downloads and sends
   `service.restart`. Any other load failure still exits (ADR 0012).
5. **An unknown voice falls back.** Home Assistant keeps sending its
   pipeline's voice (`af_heart`) after the TTS engine changes, and
   `check_voice` rejects a voice the engine does not serve: the assistant
   goes silent. `check_voice` now serves `tts.voice`, or the engine's first
   voice, and logs a warning naming both. This changes ADR 0007's HTTP
   behaviour (an OpenAI client's `alloy` gets the configured voice, not a
   400) and `api/routes_tts.py`'s module docstring.

### 7. Install, update, rollback, repair, uninstall

Install downloads only the configured default models, as today. An update
downloads the new pin of each configured model; other downloaded models whose
pin changed show "Update available". An update deletes nothing, so a rollback
finds its pins cached, and its `voiced install` fetches any the user deleted.
Repair never touches models; uninstall's models checkbox uses section 3.

## Alternatives considered

- **Per-engine package sets installed on demand** (a per-extra hashed export
  into the active environment). It ends "one lock hash identifies the
  environment": `install.json` records the extras, update and rollback replay
  them, repair reinstalls each, `hello` compares the set, and packages land
  in an environment the services import from. It saves at most 183 MB today.
  Kept for the first torch engine.
- **A separate environment per engine.** The plists run `~/.voiced/bin/voiced`,
  one link to one environment; per-engine environments need a link per
  service, another root step, and an update, rollback and repair each. It is
  the only way to run chatterbox or VoxCPM beside today's lock, so it returns
  with them.
- **A `models.list` control op beside the CLI.** A second child process and
  op for the same data, a protocol change and a fake-server scenario.
- **A separate Models pane.** One more pane in a fixed window, and two places
  that switch engines with different rules.

## Consequences

- Adding an engine needs no app change. New names: `voiced models
  list|download|delete`; `CATALOGUE`, `Engine`, `Model`, `Engine.shared`; `python -m voiced.models pin`; the codes
  `model_not_downloaded`, `token_missing`, `token_invalid`, `gated_terms`;
  `missing_model` as health status, service state and watchdog kind; the
  exceptions `ModelNotDownloaded` and `ModelMissing`; `models.lock.json`
  as package data. No new control op; protocol 1 gains only added values.

### First implementation, and later

First: `CATALOGUE`, `models.lock.json`, `pin` and the test; engines' class
attributes from the catalogue and the licence per configured model; `voiced
models list|download|delete`; offline loading, `ModelNotDownloaded` and
`missing_model` through the service, `check_loaded`, the watchdog and the
supervisor; `model_not_downloaded` in `apply_settings` and `models delete`;
the voice fallback; the `doctor` line; the model rows; uninstall through the
new delete. Until the token sheet and keychain arrive with the first gated
model asked for, a gated row says "Needs a Hugging Face token: accept the
terms (link), run `hf auth login` in Terminal, then Download", and
`token_missing` and `gated_terms` show the same.

Later, each when needed:

- A static mirror for Hugging Face outages and blocked networks, checked
  against the lock's per-file hashes, as cjpais/Handy does at commit 93c9ee0:
  mirror URLs `{mirror}/{repo_id}/{revision}/{filename}` with a sha256 check
  per file (`src-tauri/src/catalog/mod.rs`), licences per model in
  `catalog.json` beside it. Gated models are never mirrored.
- Several downloads at once; voice previews; pruning older locked revisions
  once no kept environment pins them.
- Per-engine packages or environments, with the first torch engine.

## Implementation notes (2026-10-20)

The first cut is in two parts: the catalogue, the lock, `voiced models`,
offline services, `missing_model` and the settings checks first; the app's
model rows (section 5) in a second change that reads `voiced models list
--json` and runs `download --json-progress` and `delete --yes --json`, whose
shapes are in `docs/operations.md`. Where the code differs from the text
above:

- `Engine` carries its `kind` (`stt` or `tts`). The catalogue is read by
  code that must not import the engines (the config defaults, the watchdog's
  settings check, the bundle script), and a test checks it against the
  registry group. The config's default models come from the catalogue
  (`DEFAULT_ENGINES` and the first model of each), so they have one home.
- The field is `license`, as on the engines and in `/v1/models`.
- Models are keyed by an id, what `stt.model` and `tts.model` hold, not by
  repository: an id defaults to the repository, and models that are
  folders of one repository (Moonshine's sizes, a coming engine) each get
  their own id and patterns. The lock is keyed by id and records the
  repository; `voiced models` takes ids. A revision is deleted whole, so
  `delete` refuses one that another downloaded model shares.
- The engines' class attributes come from the catalogue's default model as
  described; the per-model licence is set in one place,
  `registry.create_stt_engine` and `create_tts_engine`, which the services
  and the models step use, rather than in each app module.
- `gradium/phonon-40m-multilingual` is not catalogued: its terms were not
  granted to the account that pins, so its files could not be hashed. The
  engine refuses it, as it refuses any `gradium/phonon-*` model the
  catalogue does not list, naming it as not in the catalogue. Pinning it
  later is one catalogue line and one `pin`.
- Phonon's voices are pinned as the 37 English presets the engine loads
  (patterns naming each file), not `voices/*`: each file is its own request,
  and all 102 took 78 s here against about 30 s for the 38.
- `pin` keeps a repository's locked revision unless `--revision` is given,
  so a pattern change does not move a model; without arguments it fixes
  every repository that is new or has new patterns, and moves a model the
  catalogue dropped to the lock's `retired` list, which uninstall still
  reads.
- `voiced models download` runs in the CLI's own process (it is the
  environment's `voiced`), and its byte reports go through the installer's
  `Progress`, so the events are the installer's. A shared repository is not
  downloaded or deleted alone. `delete` refuses the model in use with the
  code `in_use`, and the status page form answers `model_not_downloaded`
  with 409.
- `check_loaded` raises `ModelNotDownloaded`, which `classify` counts as
  busy (a Wyoming client gets the busy `error` event); HTTP maps it to 503
  `model_not_downloaded` without `Retry-After`.
- A plugin engine the catalogue does not know still downloads in its own
  `load()` during the models step; a catalogued engine with a model outside
  the catalogue fetches that model whole at `main`.
- Revised 2026-10-21: `voiced.models` is a package whose
  `catalogue.py` holds only the data a new engine adds; one classifier
  (`voiced.models.classify`) turns any failed download into a code, and a
  401 for a repository that does not exist is `failed`, not a token code;
  `voiced models download` and the install's models step share
  `voiced.models.fetch`, which checks the disk first; `missing_files`
  compares sizes, so a truncated file is missing (the lock's sha256 is kept
  for the mirror under "Later", not checked when loading); the lock keeps a
  dropped model's revisions under `retired`; a file still short after a
  download is fetched again, and the download fails if it stays short;
  Cancel exits at once (exit 143)
  instead of waiting for the files in flight. Deleting one of several models
  that are folders of one repository deletes the others with it (the
  revision goes whole), refused while one of them is in use. A missing
  shared model is reported as its model, naming the shared one, so the
  command given is one `voiced models download` takes. The delete note
  reads "Phonon's shared files (gradium/phonon-voices) are deleted with
  it." rather than section 5's "Phonon's voices are deleted with it.": the
  catalogue names shared models, not what they hold.

### The app side (2026-10-21)

The model rows landed in the menu bar app as section 5 describes, with the
token sheet and keychain still "Later". Where the code differs from the
text above:

- The rows mark the model in use from the settings the control socket
  pushes, not the list's `in_use`: both follow the config file, and the
  settings change without running the list again.
- Use sends `settings.set`, then restarts that service itself; the row in
  use reads "In use, loading" until `status` says it is warm.
- The delete confirmation's "frees" is the revision's `on_disk_bytes`, plus
  the engine's shared repositories for its last downloaded model: the list
  carries no expected freed size. Its notes follow the CLI's wording.
- "Update available" offers "Update (size)", which is Download; a plugin
  engine without catalogued models has one row with Use only.
- `token_missing` and `gated_terms` show the gated row's `hf auth login`
  text with the model card; `token_invalid` asks to log in again.
- A model download has no stall timeout, unlike the installer's: the row
  has Cancel. While it runs, the app's Update and Repair are not offered.
- The fake control server reads a fake cache file that a fake `voiced
  models` (`voiced-fake-control models ...`) changes, so `model_not_downloaded`
  and `missing_model` come from the same state the rows show. A debug build
  runs the fake (or a checkout's `voiced`) in place of `~/.voiced/bin/voiced`
  when the `VOICED_EXECUTABLE` environment variable names it: the
  environment, like `VOICED_CONTROL_SOCKET`, since the fake finds its cache
  through the socket path it inherits; ADR 0018's `installSource` user
  default stays the installer's. Release builds compile both out.
- Download restarts a service that reports `missing_model` for that model,
  from the pane's row in use as from the menu window: the watchdog leaves
  the state alone, since a restart cannot download.
