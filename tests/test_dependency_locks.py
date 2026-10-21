"""The dependency-locks workflow commits every file ``scripts/refresh_locks.py`` writes."""

from __future__ import annotations

import re
from pathlib import Path

from tests.scripts_support import load

WORKFLOW = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "dependency-locks.yml"


def test_the_workflow_carries_every_generated_file() -> None:
    expected = {"uv.lock", "NOTICE", *load("refresh_locks").EXPORTS}
    text = WORKFLOW.read_text()
    uploaded = re.search(r"\n {10}path: \|\n((?: {12}\S+\n)+)", text)
    committed = re.search(r'\n\s+files="([^"]+)"\n', text)
    assert uploaded is not None and committed is not None
    assert set(uploaded.group(1).split()) == expected, "the upload-artifact path list"
    assert set(committed.group(1).split()) == expected, "the files the commit job copies"
