"""``python -m voiced.models``: the manifest, the install's models step, ``pin``."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

from voiced.models.cache import Write, cache_dir, fetch
from voiced.models.lockfile import manifest, pinned_revision, read_lock
from voiced.models.lookup import engine
from voiced.models.pinning import pin

# The models step -------------------------------------------------------------


def download(config: Path, write: Write | None) -> None:
    """The models step: each configured model, then each engine loaded once.
    The engine is built first, so a model it refuses fails before anything
    is fetched. A catalogued engine's model is fetched (:func:`fetch`) at
    its pin with its shared models, any other model of a catalogued engine
    whole at ``main``; a plugin engine downloads in its own ``load()``, and a
    model whose engine is not installed is fetched whole."""
    import asyncio

    from voiced.config import load_settings
    from voiced.engines.registry import UnknownEngine, create_engine

    settings = load_settings(config)
    print(f"Hugging Face cache: {cache_dir()}", flush=True)
    jobs: tuple[tuple[Literal["stt", "tts"], str, str], ...] = (
        ("stt", settings.stt.engine, settings.stt.model),
        ("tts", settings.tts.engine, settings.tts.model),
    )
    for kind, engine_name, model in jobs:
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
        description="Print the default models as JSON, download the configured ones, or pin.",
    )
    sub = parser.add_subparsers(dest="command")
    fetch = sub.add_parser("download", help="download the configured models and load each once")
    fetch.add_argument("--config", type=Path, required=True)
    fetch.add_argument("--report", action="store_true", help="write byte progress lines")
    pins = sub.add_parser("pin", help="lock catalogued models (needs the network)")
    pins.add_argument("models", nargs="*", help="default: every one the lock lacks or has stale")
    pins.add_argument("--revision", help="branch, tag or commit (default: the pin, else main)")
    args = parser.parse_args(argv)
    if args.command == "download":
        download(args.config, (lambda line: print(line, flush=True)) if args.report else None)
        return 0
    if args.command == "pin":
        for model_id in pin(args.models, args.revision):
            entry = read_lock().get(model_id)
            print(f"{model_id} {entry.revision if entry else 'removed'}")
        return 0
    json.dump(manifest(), sys.stdout)
    sys.stdout.write("\n")
    return 0
