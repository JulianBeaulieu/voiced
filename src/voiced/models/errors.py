"""What can go wrong with a model, and the one place hub errors are read.

:func:`classify` turns any failure of a download into an
:data:`~voiced.launchd.progress.ErrorCode` and a message for people;
:func:`hub_missing_model` says whether a failed load means the model is
not downloaded. Both follow an exception's explicit causes (``raise ...
from``), never ``__context__``: an error a library swallowed while failing
for another reason must not decide what the failure is.
"""

from __future__ import annotations

import errno
from collections.abc import Iterator

from voiced.launchd.progress import ErrorCode
from voiced.models.lockfile import pinned_revision
from voiced.models.lookup import required


class ModelsError(Exception):
    """A download or delete that failed or was refused, with the ``code``
    ``--json-progress`` and ``--json`` report."""

    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(message)
        self.code: ErrorCode = code
        self.message = message


class ModelNotDownloaded(LookupError):
    """A model's files are not in the cache. ``revision`` is None for a
    model outside the catalogue, which loads ``main``; ``missing`` names a
    shared model it needs that is the one missing (Phonon's voices), since
    the command downloads the model, not that."""

    def __init__(
        self, model_id: str, revision: str | None = None, missing: tuple[str, ...] = ()
    ) -> None:
        self.model_id = model_id
        self.revision = revision
        self.missing = missing
        self.command = f"voiced models download {model_id}"
        if missing:
            verb = "is" if len(missing) == 1 else "are"
            what = f"{model_id} needs {', '.join(missing)}, which {verb} not downloaded"
        else:
            what = f"{model_id} is not downloaded" + (
                f" at revision {revision}" if revision else ""
            )
        super().__init__(f"{what}; run {self.command}")


def causes(exc: BaseException) -> Iterator[BaseException]:
    """``exc``, then what it was raised from, and so on."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__


def hub_missing_model(exc: BaseException, model_id: str) -> ModelNotDownloaded | None:
    """``exc`` as :class:`ModelNotDownloaded` when it means the model the
    engine was loading (``model_id``) is not downloaded: that itself, or the
    hub's offline and not-found errors. A shared model it needs that is
    missing is reported as the model, naming the shared one, so the
    command given is one ``voiced models download`` takes. None for any
    other failure."""
    try:
        from huggingface_hub.errors import (
            EntryNotFoundError,
            LocalEntryNotFoundError,
            OfflineModeIsEnabled,
            RepositoryNotFoundError,
            RevisionNotFoundError,
        )

        hub_errors: tuple[type[BaseException], ...] = (
            LocalEntryNotFoundError,
            OfflineModeIsEnabled,
            RepositoryNotFoundError,
            RevisionNotFoundError,
            EntryNotFoundError,
        )
    except ImportError:
        hub_errors = ()
    for current in causes(exc):
        if isinstance(current, ModelNotDownloaded):
            if current.model_id != model_id and current.model_id in required(model_id):
                return ModelNotDownloaded(
                    model_id, pinned_revision(model_id), (current.model_id, *current.missing)
                )
            return current
        if isinstance(current, hub_errors):
            return ModelNotDownloaded(model_id, pinned_revision(model_id))
    return None


def classify(exc: BaseException, repo: str) -> tuple[ErrorCode, str]:
    """The code and message for a failed download of ``repo``."""
    try:
        from huggingface_hub import get_token
        from huggingface_hub.errors import (
            GatedRepoError,
            LocalEntryNotFoundError,
            OfflineModeIsEnabled,
            RepositoryNotFoundError,
        )
    except ImportError:  # pragma: no cover - the environment always has it
        return "failed", str(exc) or type(exc).__name__
    url = f"https://huggingface.co/{repo}"
    for current in causes(exc):
        if isinstance(current, ModelsError):
            return current.code, current.message
        if isinstance(current, GatedRepoError):
            status = current.response.status_code if current.response is not None else 401
            if status == 403:
                return "gated_terms", f"Accept the terms for {repo} on Hugging Face: {url}"
            if get_token():
                return "token_invalid", (
                    f"Hugging Face rejected the token for {repo}; run `hf auth login` again"
                )
            return "token_missing", (
                f"{repo} is gated: accept its terms at {url} while signed in, then run "
                "`hf auth login` with HF_HOME set to the Hugging Face home voiced uses"
            )
        if isinstance(current, RepositoryNotFoundError):  # a 401 here is a name typo
            return "failed", f"{repo} was not found on Hugging Face"
        if isinstance(current, LocalEntryNotFoundError | OfflineModeIsEnabled):
            return "network", f"Hugging Face could not be reached for {repo}: {current}"
        if isinstance(current, ConnectionError | TimeoutError) or type(
            current
        ).__module__.startswith(("httpx", "httpcore", "requests")):
            return "network", f"Hugging Face could not be reached for {repo}: {current}"
        if isinstance(current, OSError) and current.errno == errno.ENOSPC:
            return "disk", f"the disk is full: {current}"
    return "failed", str(exc) or type(exc).__name__
