"""The dependency-locks workflow commits every file ``scripts/refresh_locks.py`` writes."""

from __future__ import annotations

import re
from pathlib import Path

from tests.scripts_support import load

WORKFLOW = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "dependency-locks.yml"


def test_the_workflow_carries_every_generated_file() -> None:
    """``GENERATED_FILES`` is the one list both jobs use."""
    expected = {"uv.lock", "NOTICE", *load("refresh_locks").EXPORTS}
    text = WORKFLOW.read_text()
    listed = re.search(r"\n  GENERATED_FILES: \|-\n((?: {4}\S+\n)+)", text)
    assert listed is not None, "GENERATED_FILES is not a block of paths"
    assert set(listed.group(1).split()) == expected
    assert "path: ${{ env.GENERATED_FILES }}" in text, "regenerate uploads another list"
    assert "set -- $GENERATED_FILES" in text, "commit copies another list"
