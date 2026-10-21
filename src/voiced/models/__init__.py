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
``lockfile``, ``errors``, ``cache`` (snapshot, download, delete), ``pinning`` and
``commands`` (``python -m voiced.models``). This package exports what the
engines, the services and the CLI use (``__all__``); pinning and the
models step are imported from their modules, and tests patch a name in
the module that defines it.
"""

from __future__ import annotations

from voiced.models.cache import (
    DEFAULT_HF_HOME,
    CachedRevision,
    cache_dir,
    cached_revisions,
    delete_revisions,
    fetch,
    hub_dir,
    load_pinned,
    missing_files,
    not_downloaded,
    repo_folder,
    size_text,
    snapshot,
)
from voiced.models.catalogue import CATALOGUE, DEFAULT_ENGINES, Model
from voiced.models.errors import ModelNotDownloaded, ModelsError, classify, hub_missing_model
from voiced.models.lockfile import (
    Locked,
    lock,
    locked,
    pinned_revision,
    pinned_revisions,
    resolve,
)
from voiced.models.lookup import (
    catalogued,
    default_model,
    default_models,
    engine,
    find,
    is_shared,
    license_of,
    required,
)

__all__ = [
    "CATALOGUE",
    "DEFAULT_ENGINES",
    "DEFAULT_HF_HOME",
    "CachedRevision",
    "Locked",
    "Model",
    "ModelNotDownloaded",
    "ModelsError",
    "cache_dir",
    "cached_revisions",
    "catalogued",
    "classify",
    "default_model",
    "default_models",
    "delete_revisions",
    "engine",
    "fetch",
    "find",
    "hub_dir",
    "hub_missing_model",
    "is_shared",
    "license_of",
    "load_pinned",
    "lock",
    "locked",
    "missing_files",
    "not_downloaded",
    "pinned_revision",
    "pinned_revisions",
    "repo_folder",
    "required",
    "resolve",
    "size_text",
    "snapshot",
]
