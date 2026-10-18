"""Prints install-manifest.json for bundle.sh (ADR 0018, "What the app ships and pins").

    manifest.py --wheel W --lock L --uv-lock U --models M --python 3.12.15

The wheel and lock are named with their sha256; the models come from M (the
output of `python -m voiced.models`). The download size of the locked wheels
is summed from uv.lock: for each lock entry installed on an Apple Silicon Mac
running macOS 14 (the app's floor) with that CPython, the file uv would pick,
the most specific compatible wheel, else the sdist. The welcome screen shows
it; nothing checks it, so an estimate that errs by a wheel is harmless.

Needs `packaging` (bundle.sh runs this with `uv run --with packaging==...`).
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

from packaging.markers import Marker
from packaging.tags import Tag, compatible_tags, cpython_tags, mac_platforms
from packaging.utils import canonicalize_name, parse_wheel_filename

MACOS = (14, 0)
REQUIREMENT = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s;\\]+)\s*(?:;\s*([^\\]+?))?\s*\\?$")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def target(python: str) -> dict[str, str]:
    major, minor = python.split(".")[:2]
    return {
        "implementation_name": "cpython",
        "platform_machine": "arm64",
        "platform_python_implementation": "CPython",
        "platform_system": "Darwin",
        "python_full_version": python,
        "python_version": f"{major}.{minor}",
        "sys_platform": "darwin",
        "os_name": "posix",
    }


def installed(lock: Path, python: str) -> list[tuple[str, str]]:
    environment = target(python)
    found = []
    for line in lock.read_text().splitlines():
        match = REQUIREMENT.match(line)
        if match is None:
            continue
        name, version, marker = match.groups()
        if marker is None or Marker(marker).evaluate(dict(environment)):
            found.append((canonicalize_name(name), version))
    return found


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
        filename = wheel["url"].rsplit("/", 1)[-1]
        rank = min(
            (ranks[t] for t in parse_wheel_filename(filename)[3] if t in ranks), default=None
        )
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
    for model in found:
        if not re.fullmatch(r"[0-9a-f]{40}", str(model.get("revision", ""))):
            raise SystemExit(f"{model.get('repo')}: the revision must be a commit SHA")
        if not isinstance(model.get("bytes"), int) or model["bytes"] <= 0:
            raise SystemExit(f"{model.get('repo')}: no size")
    return [{"repo": m["repo"], "revision": m["revision"], "bytes": m["bytes"]} for m in found]


def main() -> int:
    parser = argparse.ArgumentParser()
    for option in ("--wheel", "--lock", "--uv-lock", "--models"):
        parser.add_argument(option, type=Path, required=True)
    parser.add_argument("--python", required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"\d+\.\d+\.\d+", args.python):
        raise SystemExit(f"--python {args.python}: a patch version is needed, such as 3.12.15")
    manifest = {
        "python": args.python,
        "lock_sha256": sha256(args.lock),
        "wheel": {"name": args.wheel.name, "sha256": sha256(args.wheel)},
        "wheels_bytes": wheels_bytes(args.lock, args.uv_lock, args.python),
        "models": models(args.models),
    }
    json.dump(manifest, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
