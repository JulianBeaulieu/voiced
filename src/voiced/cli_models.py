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
import os
import signal
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from types import FrameType
from typing import Any

from voiced import models
from voiced.config import Settings
from voiced.launchd.progress import ErrorCode, Progress
from voiced.layout import install_lock
from voiced.models import CachedRevision, Locked, Model, ModelsError, size_text

Echo = Callable[[str], None]

BUSY = "voiced is installing or updating; try again when it ends"
REF_NOTE = (
    "This model is also cached as {refs}, perhaps by another app; "
    "that app downloads it again if it needs it."
)


def add_models_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = sub.add_parser("models", help="list, download or delete models")
    commands = parser.add_subparsers(dest="models_command", required=True)
    listing = commands.add_parser("list", help="what can be downloaded, what is on disk")
    listing.add_argument("--json", action="store_true", help="machine-readable output")
    fetch = commands.add_parser("download", help="download a model at its pinned revision")
    fetch.add_argument("model", help="a model id as `voiced models list` shows it (its repository)")
    fetch.add_argument(
        "--json-progress",
        action="store_true",
        help="progress as JSON lines on stdout (ADR 0018); human output goes to stderr",
    )
    delete = commands.add_parser("delete", help="delete a downloaded model voiced pins")
    delete.add_argument("model", help="a model id as `voiced models list` shows it")
    delete.add_argument("--previous", action="store_true", help="only its older pinned revisions")
    delete.add_argument("--yes", action="store_true", help="do not ask first")
    delete.add_argument("--json", action="store_true", help="machine-readable output")


def run_models(args: argparse.Namespace, settings: Settings, echo: Echo = print) -> int:
    if args.models_command == "list":
        return cmd_list(settings, echo, as_json=args.json)
    if args.models_command == "download":
        return cmd_download(args.model, settings, echo, json_progress=args.json_progress)
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
    """The configured models and the shared models they need."""
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
        "id": model.id,
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
        "in_use": model.id in used,
        "quality": model.quality,
        "speed": model.speed,
        "note": model.note,
        "recommended": model in models.defaults(),
    }


def listing(settings: Settings) -> dict[str, Any]:
    """The ``voiced models list --json`` view."""
    from voiced.engines.catalogue import engine_label, engines_list

    hub_path = hub(settings)
    installed = {(kind, e["name"]): e for kind, entries in engines_list().items() for e in entries}
    scan = models.cached_revisions(hub_path, {m.repo for m in models.catalogued()})
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
                "models": [view(m, lock[m.id], shared=False) for m in engine.models],
                "shared": [view(m, lock[m.id], shared=True) for m in engine.shared],
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


def _rating(n: int | None) -> str:
    return "-" if n is None else f"{n}/5"


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
            name = model["id"] + (" (shared)" if model["shared"] else "")
            rows.append(
                (
                    engine["name"],
                    name,
                    model["license"],
                    _rating(model["quality"]),
                    _rating(model["speed"]),
                    f"{size_text(size)} {where}",
                    _state(model),
                )
            )
    echo(render_table(("ENGINE", "MODEL", "LICENSE", "QUALITY", "SPEED", "SIZE", "STATE"), rows))
    echo("")
    echo(f"voiced uses {size_text(view['total_bytes'])} in {view['cache']}.")
    echo("This folder is shared with other apps; voiced deletes only its own models.")
    return 0


# download ------------------------------------------------------------------

CANCELLED = "terminated (SIGTERM); downloading again resumes it"


@contextlib.contextmanager
def cancel_on_sigterm(progress: Progress, echo: Echo) -> Iterator[None]:
    """SIGTERM (the app's Cancel) ends the process at once, with an
    ``error`` event: raising in the main thread would wait for every file the
    download threads have in flight, a 2.5 GB one included. The partial
    files stay and the next download resumes them."""

    def handler(signum: int, frame: FrameType | None) -> None:
        progress.close_with_error(CANCELLED)
        echo(f"download {CANCELLED}")
        for stream in (sys.stdout, sys.stderr):
            stream.flush()
        os._exit(128 + signal.SIGTERM)

    previous = signal.signal(signal.SIGTERM, handler)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


def cmd_download(model_id: str, settings: Settings, echo: Echo, *, json_progress: bool) -> int:
    """With ``--json-progress`` stdout carries only ADR 0018's events: ``hello``,
    ``download`` per byte report, then ``done`` or ``error`` with a ``code``."""
    progress = Progress(sys.stdout if json_progress else None)
    if json_progress:
        echo = functools.partial(print, file=sys.stderr)
    write = None
    if json_progress:

        def write(line: str) -> None:  # the byte reports become download events
            progress.child_line(line)

    progress.hello()
    with cancel_on_sigterm(progress, echo):
        try:
            with models_lock(settings):
                if models.find(model_id) is None and not models.is_shared(model_id):
                    echo(
                        f"{model_id} is not in voiced's catalogue: downloading it whole at "
                        "`main`, unpinned; voiced never deletes it"
                    )
                echo(f"downloading {model_id} into {models.cache_dir()}")
                models.fetch(model_id, write)
        except KeyboardInterrupt:
            progress.close_with_error("interrupted")
            raise
        except Exception as exc:
            code, message = models.classify(exc, models.resolve(model_id).repo)
            progress.close_with_error(message, code)
            echo(f"download failed: {message}")
            return 1
        echo(f"downloaded {model_id}")
        progress.done()  # still under the handler: a SIGTERM now ends with error, not nothing
    return 0


# delete --------------------------------------------------------------------


@dataclass(frozen=True)
class Deletion:
    model: str
    revisions: list[CachedRevision]
    freed: int
    download_bytes: int
    notes: list[str]


def plan_delete(model_id: str, settings: Settings, *, previous: bool) -> Deletion:
    from voiced.engines.catalogue import engine_label

    found = models.find(model_id)
    if found is None:
        why = (
            "it is deleted with its engine's last model"
            if models.is_shared(model_id)
            else "voiced deletes only the models it pins"
        )
        raise ModelsError("failed", f"{model_id} is not a model voiced can delete: {why}")
    engine, _ = found
    used = in_use(settings)
    if not previous and model_id in used:
        raise ModelsError("in_use", f"{model_id} is in use. Choose another model first.")
    hub_path = hub(settings)
    lock = models.lock()
    entry = lock[model_id]
    going: dict[str, tuple[str, ...]] = {
        model_id: entry.previous if previous else (entry.revision,)
    }
    others = [m.id for m in engine.models if m.id != model_id]
    last = not any(_present(hub_path, lock[o], r) for o in others for r in lock[o].revisions)
    if not previous and last:
        going |= {s.id: lock[s.id].revisions for s in engine.shared}
    shared = sorted(
        m for m in going if m != model_id and any(_present(hub_path, lock[m], r) for r in going[m])
    )
    sharers = _same_download(hub_path, going, used)
    wanted = {(lock[m].repo, r) for m, revisions in going.items() for r in revisions}
    scan = models.cached_revisions(hub_path, {repo for repo, _ in wanted})
    targets = [c for c in scan.revisions if (c.repo, c.revision) in wanted]
    notes = []
    refs = sorted({ref for c in targets if c.repo == entry.repo for ref in c.refs})
    if refs:
        notes.append(REF_NOTE.format(refs=", ".join(f"`{r}`" for r in refs)))
    if shared:
        label = engine_label(engine.name)
        notes.append(f"{label}'s shared files ({', '.join(shared)}) are deleted with it.")
    if sharers:
        notes.append(
            f"{', '.join(sharers)} {'is' if len(sharers) == 1 else 'are'} in the same "
            "download and deleted with it."
        )
    freed = models.delete_revisions(hub_path, targets, dry_run=True) if targets else 0
    return Deletion(model_id, targets, freed, entry.bytes, notes)


def _same_download(hub_path: Path, going: dict[str, tuple[str, ...]], used: set[str]) -> list[str]:
    """``huggingface_hub`` deletes a revision whole, so other models that
    are folders of a repository being deleted, at the same revision, go too
    (added to ``going``); refused when one of them is in use. One pass is
    enough: what it adds shares a repository and revisions with what is
    there already."""
    lock = models.lock()
    sharing = {
        other.id: (model_id, tuple(r for r in revisions if _present(hub_path, other, r)))
        for model_id, revisions in going.items()
        for other in lock.values()
        if other.id not in going and other.repo == lock[model_id].repo
    }
    added = {other: hit for other, hit in sharing.items() if hit[1]}
    for other, (model_id, revisions) in added.items():
        if other in used:
            raise ModelsError(
                "in_use",
                f"{model_id} shares {lock[model_id].repo} at {revisions[0][:12]} with {other}, "
                "which is in use; voiced deletes a revision whole. Choose another model first.",
            )
        going[other] = revisions
    return list(added)


def _present(hub_path: Path, entry: Locked, revision: str) -> bool:
    """Any of ``entry``'s locked files cached at ``revision``."""
    return revision in entry.revisions and len(
        models.missing_files(hub_path, entry, revision)
    ) < len(entry.files)


def cmd_delete(
    args: argparse.Namespace,
    settings: Settings,
    echo: Echo,
    ask: Callable[[str], str] = input,
) -> int:
    """Exit 0 when done or there was nothing to delete, 1 when refused,
    declined or failed; with ``--json`` every failure is
    ``{"error": {"code", "message"}}`` on stdout."""
    try:
        with models_lock(settings):
            return _delete(args, settings, echo, ask)
    except KeyboardInterrupt:
        _refused(args, echo, "failed", "interrupted")
        raise
    except ModelsError as exc:
        return _refused(args, echo, exc.code, exc.message)
    except Exception as exc:
        return _refused(args, echo, "failed", str(exc) or type(exc).__name__)


def _refused(args: argparse.Namespace, echo: Echo, code: ErrorCode, message: str) -> int:
    if args.json:
        echo(json.dumps({"error": {"code": code, "message": message}}))
    else:
        echo(f"voiced models delete: {message}")
    return 1


def _delete(
    args: argparse.Namespace, settings: Settings, echo: Echo, ask: Callable[[str], str]
) -> int:
    plan = plan_delete(args.model, settings, previous=args.previous)
    if not plan.revisions:
        what = "older versions" if args.previous else "download"
        return _done(args, echo, plan, f"nothing to delete: no {what} of {plan.model} cached")
    what = "the old versions of " if args.previous else ""
    question = (
        f"Delete {what}{plan.model}? This frees {size_text(plan.freed)}. "
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
    plan = Deletion(plan.model, plan.revisions, freed, plan.download_bytes, plan.notes)
    return _done(args, echo, plan, f"deleted {plan.model}, freed {size_text(freed)}")


def _done(args: argparse.Namespace, echo: Echo, plan: Deletion, text: str) -> int:
    if args.json:
        deleted = [
            {"repo": c.repo, "revision": c.revision, "bytes": c.bytes} for c in plan.revisions
        ]
        result = {"model": plan.model, "deleted": deleted, "freed": plan.freed, "notes": plan.notes}
        echo(json.dumps(result))
    else:
        if args.yes:
            for note in plan.notes:
                echo(note)
        echo(text)
    return 0
