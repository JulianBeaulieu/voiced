"""Plain-text tables for the CLI (no borders, no color, no emoji)."""

from __future__ import annotations

from collections.abc import Sequence


def render_table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    """Left-aligned columns separated by two spaces; the last column is not padded."""
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    def fmt(cells: Sequence[str]) -> str:
        parts = [c.ljust(widths[i]) for i, c in enumerate(cells[:-1])]
        parts.append(cells[-1])
        return "  ".join(parts).rstrip()

    return "\n".join([fmt(headers), *(fmt(r) for r in rows)])
