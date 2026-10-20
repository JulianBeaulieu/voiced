"""Language tags as the text pipeline and the server compare them."""

from __future__ import annotations

__all__ = ["primary_subtag"]


def primary_subtag(tag: str) -> str:
    """The primary language subtag of a BCP-47 tag, lowercased: ``en-US``,
    ``en_GB`` and ``EN`` all give ``en``."""
    return tag.replace("_", "-").split("-")[0].lower()
