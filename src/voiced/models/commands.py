"""``python -m voiced.models``: the manifest, the install's models step, ``pin``."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Literal

from voiced.models.cache import Write, cache_dir, fetch
from voiced.models.lockfile import manifest, pinned_revision, read_lock
from voiced.models.lookup import engine
from voiced.models.pinning import pin

# The models step -------------------------------------------------------------


def download(
    config: Path, write: Write | None, changes: Mapping[str, object] | None = None
) -> None:
    """The models step: each configured model, then each engine loaded once;
    nothing for a service that is off (ADR 0033), which loads no model.
    ``changes`` (``voiced install --set``) apply on top of the config, which
    the install writes only after this step.
    The engine is built first, so a model it refuses fails before anything
    is fetched. A catalogued engine's model is fetched (:func:`fetch`) at
    its pin with its shared models, any other model of a catalogued engine
    whole at ``main``; a plugin engine downloads in its own ``load()``, and a
    model whose engine is not installed is fetched whole."""
    import asyncio

    from voiced.config import load_settings
    from voiced.engines.registry import UnknownEngine, create_engine

    settings = load_settings(config)
    if changes:
        from voiced.settings_store import stage_settings

        settings = stage_settings(changes, config).onto(settings)
    print(f"Hugging Face cache: {cache_dir()}", flush=True)
    jobs: tuple[tuple[Literal["stt", "tts"], str, str], ...] = (
        ("stt", settings.stt.engine, settings.stt.model),
        ("tts", settings.tts.engine, settings.tts.model),
    )
    for kind, engine_name, model in jobs:
        if not settings.service(kind).enabled:
            print(f"{kind}: off; nothing to download", flush=True)
            continue
        revision = pinned_revision(model)
        at = f" revision={revision}" if revision is not None else ""
        print(f"{kind}: engine={engine_name} model={model}{at}", flush=True)
        try:
            built = create_engine(kind, engine_name, model)
        except UnknownEngine as exc:
            print(f"  {exc}; downloading weights only", flush=True)
            fetch(model, write)
            continue
        if engine(engine_name) is not None:
            fetch(model, write)
        asyncio.run(built.load())
        print(f"  loaded {built.name} ({built.model_id})", flush=True)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m voiced.models",
        description="Print the model catalogue as JSON, download the configured models, or pin.",
    )
    sub = parser.add_subparsers(dest="command")
    download_parser = sub.add_parser(
        "download", help="download the configured models and load each once"
    )
    download_parser.add_argument("--config", type=Path, required=True)
    download_parser.add_argument("--report", action="store_true", help="write byte progress lines")
    download_parser.add_argument(
        "--set",
        dest="pairs",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="a setting voiced install has not written yet",
    )
    pins = sub.add_parser("pin", help="lock catalogued models (needs the network)")
    pins.add_argument("models", nargs="*", help="default: every one the lock lacks or has stale")
    pins.add_argument("--revision", help="branch, tag or commit (default: the pin, else main)")
    args = parser.parse_args(argv)
    if args.command == "download":
        from voiced.settings_store import parse_setting

        try:
            changes = dict(parse_setting(pair) for pair in args.pairs)
        except ValueError as exc:
            print(f"--set {exc}", file=sys.stderr)
            return 2
        report = (lambda line: print(line, flush=True)) if args.report else None
        download(args.config, report, changes)
        return 0
    if args.command == "pin":
        for model_id in pin(args.models, args.revision):
            entry = read_lock().get(model_id)
            print(f"{model_id} {entry.revision if entry else 'removed'}")
        return 0
    json.dump(manifest(), sys.stdout)
    sys.stdout.write("\n")
    return 0
