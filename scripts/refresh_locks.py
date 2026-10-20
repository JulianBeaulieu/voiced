"""Regenerates everything derived from uv.lock: the two shipped locks and NOTICE.

    uv run python scripts/refresh_locks.py

Run it after any change to uv.lock: after ``uv lock`` (which it runs first, so
an edit to pyproject.toml is enough). On Dependabot's pull requests, whose
change is only pyproject.toml and uv.lock, the dependency-locks workflow runs
it and pushes the result. Tests fail while any of these files says something
other than uv.lock (``tests/launchd/test_install.py`` for the locks,
``tests/test_notice.py`` for NOTICE). NOTICE asks PyPI for licences.
"""

from __future__ import annotations

import subprocess
import sys

import notice
from lock_target import ROOT

# Each shipped lock and the `uv export` flags that write it (ADR 0017, "One lock
# for all paths"). The test that keeps them in step with uv.lock uses these,
# and .github/workflows/dependency-locks.yml commits them (a test checks it).
EXPORTS = {
    "src/voiced/requirements.lock": [
        "--no-dev",
        "--extra",
        "stt",
        "--extra",
        "tts",
        "--extra",
        "phonon",
        "--extra",
        "pocket",
    ],
    "src/voiced/build-requirements.lock": ["--only-group", "build"],
}

COMMAND = "uv run python scripts/refresh_locks.py"


def export_argv(uv: str, path: str) -> list[str]:
    """Prints the lock at ``path`` as uv.lock has it."""
    return [uv, "export", "--frozen", *EXPORTS[path], "--no-emit-project", "--no-header", "-q"]


def main() -> int:
    subprocess.run(["uv", "lock", "-q"], cwd=ROOT, check=True)
    for path in EXPORTS:
        subprocess.run([*export_argv("uv", path), "-o", path], cwd=ROOT, check=True)
    return notice.main([])


if __name__ == "__main__":
    sys.exit(main())
