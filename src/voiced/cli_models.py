"""``voiced models list|download|delete`` (ADR 0022).

``list --json`` is the one view of what voiced can download, what is on
disk and what is in use, which the menu bar app reads; ``download`` fetches
a catalogued model's locked files and its engine's shared repositories with
ADR 0018's byte progress (``--json-progress``), and ``delete`` removes a
model's pinned revision (``--previous``: its older locked ones) through
``huggingface_hub``, never a path of its own. Both take the install lock
without waiting, so they never run beside an install, an update or an
uninstall. "In use" follows the config file.
"""

from __future__ import annotations

import argparse
import contextlib
import functools
import json
import shutil
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from voiced import models
from voiced.config import Settings
from voiced.layout import install_lock
from voiced.models import CachedRevision, Locked, Model, size_text

Echo = Callable[[str], None]

DISK_HEADROOM = 10**9
"""Free space a download leaves: the bytes still to fetch plus 1 GB."""

BUSY = "voiced is installing or updating; try again when it ends"
REF_NOTE = (
    "This model is also cached as {refs}, perhaps by another app; "
    "that app downloads it again if it needs it."
)


class ModelsError(Exception):
    """A refusal with a ``code`` for ``--json-progress`` and ``--json``."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def add_models_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = sub.add_parser("models", help="list, download or delete models")
    commands = parser.add_subparsers(dest="models_command", required=True)
    listing = commands.add_parser("list", help="what can be downloaded, what is on disk")
    listing.add_argument("--json", action="store_true", help="machine-readable output")
    fetch = commands.add_parser("download", help="download a model at its pinned revision")
    fetch.add_argument("repo", help="a Hugging Face repository id, as `voiced models list` shows")
    fetch.add_argument(
        "--json-progress",
        action="store_true",
        help="progress as JSON lines on stdout (ADR 0018); human output goes to stderr",
    )
    delete = commands.add_parser("delete", help="delete a downloaded model voiced pins")
    delete.add_argument("repo")
    delete.add_argument("--previous", action="store_true", help="only its older pinned revisions")
    delete.add_argument("--yes", action="store_true", help="do not ask first")
    delete.add_argument("--json", action="store_true", help="machine-readable output")


def run_models(args: argparse.Namespace, settings: Settings, echo: Echo = print) -> int:
    if args.models_command == "list":
        return cmd_list(settings, echo, as_json=args.json)
    if args.models_command == "download":
        return cmd_download(args.repo, settings, echo, json_progress=args.json_progress)
    return cmd_delete(args, settings, echo)


@contextlib.contextmanager
def models_lock(settings: Settings) -> Iterator[None]:
    """The install lock, without waiting (``busy``). No state directory:
    nothing is installed, so there is nothing to race."""
    state_dir = settings.install.venv_dir
    if not state_dir.is_dir():
        yield
        return
    try:
        with install_lock(state_dir, wait=False):
            yield
    except BlockingIOError:
        raise ModelsError("busy", BUSY) from None


def hub(settings: Settings) -> Path:
    return models.hub_dir(settings.install.hf_home)


def in_use(settings: Settings) -> set[str]:
    """The configured models and the shared repositories they need."""
    return {r for m in (settings.stt.model, settings.tts.model) for r in models.required(m)}


# list ----------------------------------------------------------------------


def _model_view(
    model: Model,
    entry: Locked,
    cached: list[CachedRevision],
    hub_path: Path,
    used: set[str],
    *,
    shared: bool,
) -> dict[str, Any]:
    by_revision = {c.revision: c for c in cached if c.repo == model.repo}
    pinned = by_revision.get(entry.revision)
    return {
        "repo": model.repo,
        "shared": shared,
        "license": model.license,
        "url": model.url,
        "gated": model.gated,
        "revision": entry.revision,
        "download_bytes": entry.bytes,
        "on_disk_bytes": pinned.bytes if pinned is not None else 0,
        "downloaded": not models.missing_files(hub_path, entry),
        "previous": [
            {"revision": rev, "bytes": by_revision[rev].bytes}
            for rev in entry.previous
            if rev in by_revision
        ],
        "refs": list(pinned.refs) if pinned is not None else [],
        "in_use": model.repo in used,
    }


def listing(settings: Settings) -> dict[str, Any]:
    """The ``voiced models list --json`` view."""
    from voiced.engines.catalogue import engine_label, engines_list

    hub_path = hub(settings)
    installed = {(kind, e["name"]): e for kind, entries in engines_list().items() for e in entries}
    scan = models.cached_revisions(hub_path, {m.repo for m in models.repositories()})
    used = in_use(settings)
    lock = models.lock()
    engines: list[dict[str, Any]] = []
    for engine in models.CATALOGUE:
        found = installed.pop((engine.kind, engine.name), None)
        view = functools.partial(_model_view, cached=scan.revisions, hub_path=hub_path, used=used)
        engines.append(
            {
                "name": engine.name,
                "label": engine_label(engine.name),
                "kind": engine.kind,
                "installed": found is not None,
                "languages": found["languages"] if found else [],
                "voices": found.get("voices", []) if found else [],
                "models": [view(m, lock[m.repo], shared=False) for m in engine.models],
                "shared": [view(m, lock[m.repo], shared=True) for m in engine.shared],
            }
        )
    for (kind, name), found in sorted(installed.items()):  # plugins: nothing to manage
        engines.append(
            {
                "name": name,
                "label": found["label"],
                "kind": kind,
                "installed": True,
                "languages": found["languages"],
                "voices": found.get("voices", []),
                "models": [],
                "shared": [],
            }
        )
    locked_revisions = models.pinned_revisions()
    total = sum(c.bytes for c in scan.revisions if (c.repo, c.revision) in locked_revisions)
    return {"cache": str(hub_path), "total_bytes": total, "engines": engines}


def _state(model: dict[str, Any]) -> str:
    if model["in_use"]:
        state = "in use" if model["downloaded"] else "in use, not downloaded"
    elif model["downloaded"]:
        state = "downloaded"
    elif model["previous"]:
        state = "update available"
    else:
        state = "not downloaded"
    old = sum(p["bytes"] for p in model["previous"])
    if old:
        state += f"; old version {size_text(old)}"
    return state + ("; gated" if model["gated"] and not model["downloaded"] else "")


def cmd_list(settings: Settings, echo: Echo, *, as_json: bool) -> int:
    from voiced.table import render_table

    view = listing(settings)
    if as_json:
        echo(json.dumps(view, indent=2))
        return 0
    rows = []
    for engine in view["engines"]:
        for model in [*engine["models"], *engine["shared"]]:
            size = model["on_disk_bytes"] if model["downloaded"] else model["download_bytes"]
            where = "on disk" if model["downloaded"] else "download"
            name = model["repo"] + (" (shared)" if model["shared"] else "")
            rows.append(
                (
                    engine["name"],
                    name,
                    model["license"],
                    f"{size_text(size)} {where}",
                    _state(model),
                )
            )
    echo(render_table(("ENGINE", "MODEL", "LICENSE", "SIZE", "STATE"), rows))
    echo("")
    echo(f"voiced uses {size_text(view['total_bytes'])} in {view['cache']}.")
    echo("This folder is shared with other apps; voiced deletes only its own models.")
    return 0


# download ------------------------------------------------------------------


def remaining_bytes(hub_path: Path, repos: list[str]) -> int:
    """What is still to fetch of ``repos`` (0 for an unpinned one, whose size is unknown)."""
    total = 0
    for repo in repos:
        entry = models.locked(repo)
        if entry is not None:
            total += sum(entry.files[name].bytes for name in models.missing_files(hub_path, entry))
    return total


def check_disk(hub_path: Path, needed: int) -> None:
    existing = next(p for p in (hub_path, *hub_path.parents) if p.exists())
    free = shutil.disk_usage(existing).free
    if free < needed + DISK_HEADROOM:
        raise ModelsError(
            "disk",
            f"not enough disk space in {hub_path}: the download needs {size_text(needed)} "
            f"and 1 GB to spare, {size_text(free)} is free",
        )


def error_code(exc: BaseException) -> str:
    """The ``code`` of an ``error`` event for what ended a download."""
    try:
        from huggingface_hub import get_token
        from huggingface_hub.errors import GatedRepoError, HfHubHTTPError, LocalEntryNotFoundError
    except ImportError:  # pragma: no cover - the environment always has it
        return "failed"
    current: BaseException | None = exc
    while current is not None:
        if isinstance(current, ModelsError):
            return current.code
        if isinstance(current, HfHubHTTPError) and current.response is not None:
            status = current.response.status_code
            if status == 401:
                return "token_invalid" if get_token() else "token_missing"
            if status == 403 and isinstance(current, GatedRepoError):
                return "gated_terms"
        if isinstance(current, LocalEntryNotFoundError | ConnectionError | TimeoutError):
            return "network"
        if type(current).__module__.startswith(("httpx", "httpcore", "requests")):
            return "network"
        if isinstance(current, OSError) and current.errno == 28:  # ENOSPC
            return "disk"
        current = current.__cause__ or current.__context__
    return "failed"


def cmd_download(repo: str, settings: Settings, echo: Echo, *, json_progress: bool) -> int:
    """With ``--json-progress`` stdout carries only ADR 0018's events: ``hello``,
    ``download`` per byte report, then ``done`` or ``error`` with a ``code``.
    SIGTERM (the app's Cancel) ends it with an ``error``; the partly
    downloaded files resume it next time."""
    from voiced.launchd.progress import Progress, exit_on_sigterm

    progress = Progress(sys.stdout if json_progress else None)
    if json_progress:
        echo = functools.partial(print, file=sys.stderr)
    write: Callable[[str], None] | None = None
    if json_progress:

        def write(line: str) -> None:
            progress.child_line(line)

    with exit_on_sigterm():
        progress.hello()
        try:
            with models_lock(settings):
                if models.find(repo) is None:
                    if repo in {m.repo for m in models.repositories()}:
                        raise ModelsError(
                            "failed", f"{repo} comes with its engine's models; download one of them"
                        )
                    echo(
                        f"{repo} is not in voiced's catalogue: downloading it whole at `main`, "
                        "unpinned; voiced never deletes it"
                    )
                repos = models.required(repo)
                hub_path = hub(settings)
                check_disk(hub_path, remaining_bytes(hub_path, repos))
                for name in repos:
                    echo(f"downloading {name} into {hub_path}")
                    models.snapshot(name, write=write)
            echo(f"downloaded {repo}")
            progress.done()
        except BaseException as exc:
            if isinstance(exc, KeyboardInterrupt):
                raise
            message = str(exc) or type(exc).__name__
            progress.error(message, error_code(exc))
            echo(f"download failed: {message}")
            return 1
    return 0


# delete --------------------------------------------------------------------


@dataclass(frozen=True)
class Deletion:
    repo: str
    revisions: list[CachedRevision]
    freed: int
    download_bytes: int
    notes: list[str]


def plan_delete(repo: str, settings: Settings, *, previous: bool) -> Deletion:
    found = models.find(repo)
    if found is None:
        catalogued = {m.repo for m in models.repositories()}
        why = (
            "it is deleted with its engine's last model"
            if repo in catalogued
            else "voiced deletes only the models it pins"
        )
        raise ModelsError("failed", f"{repo} is not a model voiced can delete: {why}")
    engine, _ = found
    if not previous and repo in in_use(settings):
        raise ModelsError("in_use", f"{repo} is in use. Choose another model first.")
    hub_path = hub(settings)
    entry = models.lock()[repo]
    wanted = {(repo, rev) for rev in entry.previous} if previous else {(repo, entry.revision)}
    repos = {repo, *(s.repo for s in engine.shared)}
    scan = models.cached_revisions(hub_path, repos | {m.repo for m in engine.models})
    shared_go = False
    if not previous:
        others = {(m.repo, r) for m in engine.models if m.repo != repo for r in _locked(m.repo)}
        shared_go = not any((c.repo, c.revision) in others for c in scan.revisions)
        if shared_go:
            wanted |= {(s.repo, r) for s in engine.shared for r in _locked(s.repo)}
    targets = [c for c in scan.revisions if (c.repo, c.revision) in wanted]
    notes = []
    refs = sorted({ref for c in targets if c.repo == repo for ref in c.refs})
    if refs:
        notes.append(REF_NOTE.format(refs=", ".join(f"`{r}`" for r in refs)))
    shared = [c.repo for c in targets if c.repo != repo]
    if shared:
        label = engine.name[:1].upper() + engine.name[1:]
        notes.append(f"{label}'s shared files ({', '.join(sorted(set(shared)))}) go with it.")
    freed = models.delete_revisions(hub_path, targets, dry_run=True) if targets else 0
    return Deletion(repo, targets, freed, entry.bytes, notes)


def _locked(repo: str) -> tuple[str, ...]:
    entry = models.lock()[repo]
    return (entry.revision, *entry.previous)


def cmd_delete(
    args: argparse.Namespace,
    settings: Settings,
    echo: Echo,
    ask: Callable[[str], str] = input,
) -> int:
    try:
        with models_lock(settings):
            plan = plan_delete(args.repo, settings, previous=args.previous)
            if not plan.revisions:
                what = "older versions" if args.previous else "download"
                return _done(
                    args, echo, plan, f"nothing to delete: no {what} of {plan.repo} cached"
                )
            what = "the old versions of " if args.previous else ""
            question = (
                f"Delete {what}{plan.repo}? This frees {size_text(plan.freed)}. "
                f"Downloading it again takes {size_text(plan.download_bytes)}."
            )
            if not args.yes:
                if args.json:
                    raise ModelsError("failed", "pass --yes with --json")
                for line in (question, *plan.notes):
                    echo(line)
                try:
                    answer = ask("Delete? [y/N] ")
                except EOFError:
                    answer = ""
                if answer.strip().lower() not in ("y", "yes"):
                    echo("nothing deleted")
                    return 1
            freed = models.delete_revisions(hub(settings), plan.revisions)
            plan = Deletion(plan.repo, plan.revisions, freed, plan.download_bytes, plan.notes)
            return _done(args, echo, plan, f"deleted {plan.repo}, freed {size_text(freed)}")
    except ModelsError as exc:
        if args.json:
            echo(json.dumps({"error": {"code": exc.code, "message": str(exc)}}))
        else:
            echo(f"voiced models delete: {exc}")
        return 1


def _done(args: argparse.Namespace, echo: Echo, plan: Deletion, text: str) -> int:
    if args.json:
        deleted = [
            {"repo": c.repo, "revision": c.revision, "bytes": c.bytes} for c in plan.revisions
        ]
        echo(
            json.dumps(
                {"repo": plan.repo, "deleted": deleted, "freed": plan.freed, "notes": plan.notes}
            )
        )
    else:
        if args.yes:
            for note in plan.notes:
                echo(note)
        echo(text)
    return 0
