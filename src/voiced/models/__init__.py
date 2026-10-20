"""The models voiced knows, pinned per release, and their download (ADR 0022).

Two parts, in the relation of ``pyproject.toml`` to ``uv.lock``:

* :data:`CATALOGUE`, written by hand: each engine voiced ships, its models
  (the first is the default) and the models every model of it needs
  (``shared``), each with its Hugging Face repository, licence, whether it
  is gated, and the files the engine loads (``patterns``). A model is known
  by its ``id``, what ``stt.model`` and ``tts.model`` hold: its repository,
  unless several models are folders of one repository, when each has an
  ``id`` of its own.
* ``models.lock.json`` in this package, written by ``python -m
  voiced.models pin [MODEL ...]`` (a maintainer's command, which needs the
  network): per model its repository, the commit, the patterns it was
  resolved with, each file's size and sha256, and ``previous``, the
  revisions earlier releases pinned, so uninstall still knows them. A test
  fails, naming the command, when the two disagree.

Every catalogued engine loads its model from :func:`snapshot` with
``check_only``: the services never download (they run with
``HF_HUB_OFFLINE``), and a missing file is :class:`ModelNotDownloaded`. A
model the catalogue does not know loads its ``main`` revision, as before.

``python -m voiced.models`` prints the default models as JSON (the app's
``install-manifest.json`` is written from it); ``python -m voiced.models
download --config FILE [--report]`` is the models step of ``voiced
install``, run in the new environment: it fetches each configured model and
loads each engine once. With ``--report`` it also writes
:data:`voiced.launchd.progress.DOWNLOAD_PREFIX` lines that the installer
turns into ``download`` events.

Standard library only at import (the app's bundle script runs this from the
unpacked wheel, and the watchdog checks settings with it); the download and
the cache scans import ``huggingface_hub``.

The package: ``catalogue`` (the data a new engine adds to), ``lookup``,
``lockfile``, ``cache`` (snapshot, download, delete), ``pinning`` and
``commands`` (``python -m voiced.models``). Everything is importable from
``voiced.models``.
"""

from __future__ import annotations

from voiced.models.cache import (
    BYTES_BAR as BYTES_BAR,
)
from voiced.models.cache import (
    REPORT_INTERVAL_S as REPORT_INTERVAL_S,
)
from voiced.models.cache import (
    ByteReport as ByteReport,
)
from voiced.models.cache import (
    CachedRevision as CachedRevision,
)
from voiced.models.cache import (
    CacheScan as CacheScan,
)
from voiced.models.cache import (
    ModelNotDownloaded as ModelNotDownloaded,
)
from voiced.models.cache import (
    Write as Write,
)
from voiced.models.cache import (
    _counting_tqdm as _counting_tqdm,
)
from voiced.models.cache import (
    cache_dir as cache_dir,
)
from voiced.models.cache import (
    cached_revisions as cached_revisions,
)
from voiced.models.cache import (
    delete_revisions as delete_revisions,
)
from voiced.models.cache import (
    hub_dir as hub_dir,
)
from voiced.models.cache import (
    hub_missing_model as hub_missing_model,
)
from voiced.models.cache import (
    missing_files as missing_files,
)
from voiced.models.cache import (
    not_downloaded as not_downloaded,
)
from voiced.models.cache import (
    repo_folder as repo_folder,
)
from voiced.models.cache import (
    size_text as size_text,
)
from voiced.models.cache import (
    snapshot as snapshot,
)
from voiced.models.cache import (
    snapshot_dir as snapshot_dir,
)
from voiced.models.catalogue import (
    CATALOGUE as CATALOGUE,
)
from voiced.models.catalogue import (
    DEFAULT_ENGINES as DEFAULT_ENGINES,
)
from voiced.models.catalogue import (
    PHONON as PHONON,
)
from voiced.models.catalogue import (
    PHONON_VOICES as PHONON_VOICES,
)
from voiced.models.catalogue import (
    Engine as Engine,
)
from voiced.models.catalogue import (
    Model as Model,
)
from voiced.models.commands import (
    download as download,
)
from voiced.models.commands import (
    main as main,
)
from voiced.models.lockfile import (
    LOCK_FILE as LOCK_FILE,
)
from voiced.models.lockfile import (
    PIN_COMMAND as PIN_COMMAND,
)
from voiced.models.lockfile import (
    Locked as Locked,
)
from voiced.models.lockfile import (
    LockedFile as LockedFile,
)
from voiced.models.lockfile import (
    lock as lock,
)
from voiced.models.lockfile import (
    locked as locked,
)
from voiced.models.lockfile import (
    manifest as manifest,
)
from voiced.models.lockfile import (
    pinned_revision as pinned_revision,
)
from voiced.models.lockfile import (
    pinned_revisions as pinned_revisions,
)
from voiced.models.lockfile import (
    read_lock as read_lock,
)
from voiced.models.lockfile import (
    write_lock as write_lock,
)
from voiced.models.lookup import (
    catalogued as catalogued,
)
from voiced.models.lookup import (
    default_model as default_model,
)
from voiced.models.lookup import (
    engine as engine,
)
from voiced.models.lookup import (
    find as find,
)
from voiced.models.lookup import (
    license_of as license_of,
)
from voiced.models.lookup import (
    required as required,
)
from voiced.models.pinning import (
    HubFiles as HubFiles,
)
from voiced.models.pinning import (
    fetch_sha256 as fetch_sha256,
)
from voiced.models.pinning import (
    hub_files as hub_files,
)
from voiced.models.pinning import (
    pin as pin,
)
from voiced.models.pinning import (
    pin_repo as pin_repo,
)
from voiced.models.pinning import (
    stale as stale,
)
