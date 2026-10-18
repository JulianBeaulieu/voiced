"""Prints the menu bar app's install-manifest.json (ADR 0018, "What the app ships and pins").

    python scripts/app_manifest.py --wheel W --lock L --models M [--uv-lock U]

menubar/Scripts/bundle.sh runs it. The manifest names the voiced version, the
CPython patch version (``menubar/app-python-version``), the wheel and the
lock with their sha256, the download size of the locked wheels, and the
pinned models from M, what the wheel's ``python -I -m voiced.models`` printed.

The download size is summed from uv.lock: for each lock entry installed on the
app's target Mac (``lock_target``), the file uv would pick there, the most
specific compatible wheel, else the sdist. The welcome screen shows it;
nothing checks it, so an estimate that errs by a wheel is harmless.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import tomllib
from pathlib import Path
from typing import Any

from lock_target import MACOS, ROOT, installed, python_version
from packaging.tags import Tag, compatible_tags, cpython_tags, mac_platforms
from packaging.utils import canonicalize_name, parse_wheel_filename


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def priorities(python: str) -> dict[Tag, int]:
    major, minor = (int(part) for part in python.split(".")[:2])
    platforms = list(mac_platforms(MACOS, "arm64"))
    tags = [
        *cpython_tags((major, minor), platforms=platforms),
        *compatible_tags((major, minor), platforms=platforms),
    ]
    return {tag: rank for rank, tag in enumerate(tags)}


def download_size(package: dict[str, Any], ranks: dict[Tag, int]) -> int:
    best: tuple[int, int] | None = None
    for wheel in package.get("wheels", []):
        tags = parse_wheel_filename(wheel["url"].rsplit("/", 1)[-1])[3]
        rank = min((ranks[t] for t in tags if t in ranks), default=None)
        if rank is not None and (best is None or rank < best[0]):
            best = (rank, int(wheel["size"]))
    if best is not None:
        return best[1]
    sdist = package.get("sdist")
    if sdist is None:
        raise SystemExit(f"{package['name']} {package['version']}: no file for this Mac in uv.lock")
    return int(sdist["size"])


def wheels_bytes(lock: Path, uv_lock: Path, python: str) -> int:
    packages = {
        (canonicalize_name(p["name"]), p["version"]): p
        for p in tomllib.loads(uv_lock.read_text())["package"]
    }
    ranks = priorities(python)
    total = 0
    for key in installed(lock, python):
        if key not in packages:
            raise SystemExit(f"{key[0]}=={key[1]} is in the lock but not in uv.lock")
        total += download_size(packages[key], ranks)
    return total


def models(path: Path) -> list[dict[str, Any]]:
    found = json.loads(path.read_text())["models"]
    if not found:
        raise SystemExit(f"{path}: no models")
    for model in found:
        if not re.fullmatch(r"[0-9a-f]{40}", str(model.get("revision", ""))):
            raise SystemExit(f"{model.get('repo')}: the revision must be a commit SHA")
        size = model.get("bytes")
        if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
            raise SystemExit(f"{model.get('repo')}: no size")
    return [{"repo": m["repo"], "revision": m["revision"], "bytes": m["bytes"]} for m in found]


def manifest(wheel: Path, lock: Path, uv_lock: Path, pins: Path) -> dict[str, Any]:
    python = python_version()
    name, version = parse_wheel_filename(wheel.name)[:2]
    if name != "voiced":
        raise SystemExit(f"{wheel.name} is not a voiced wheel")
    return {
        "version": str(version),
        "python": python,
        "lock_sha256": sha256(lock),
        "wheel": {"name": wheel.name, "sha256": sha256(wheel)},
        "wheels_bytes": wheels_bytes(lock, uv_lock, python),
        "models": models(pins),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    for option in ("--wheel", "--lock", "--models"):
        parser.add_argument(option, type=Path, required=True)
    parser.add_argument("--uv-lock", type=Path, default=ROOT / "uv.lock")
    args = parser.parse_args(argv)
    json.dump(manifest(args.wheel, args.lock, args.uv_lock, args.models), sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
