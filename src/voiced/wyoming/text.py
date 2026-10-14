"""Streaming sentence accumulation for the TTS listener.

:func:`voiced.text.normalize` and :func:`voiced.text.split_sentences` do the
real work; this module only adds the incremental buffering the Wyoming
streaming path needs, handing out each sentence as soon as it is complete so
synthesis can start before the conversation agent has finished writing.
"""

from __future__ import annotations

import re

from voiced.text import split_sentences

# A sentence is complete once terminal punctuation (plus optional closing
# quotes or brackets) is followed by whitespace. Anything after the last
# such boundary may still be growing and stays in the buffer.
_BOUNDARY = re.compile("[.!?]+[\"'\u201d\u2019)\\]]*\\s+")


class SentenceAccumulator:
    """Collects streamed text and hands out sentences as soon as they are complete.

    ``add`` returns the sentences that became complete with this chunk;
    ``finish`` returns whatever is left when the stream ends. Newlines are
    always boundaries so list items and headings are not glued together.
    """

    def __init__(self) -> None:
        self._buffer = ""

    def add(self, text: str) -> list[str]:
        self._buffer += text
        complete, self._buffer = _split_complete(self._buffer)
        return complete

    def finish(self) -> list[str]:
        rest, self._buffer = self._buffer, ""
        return split_sentences(rest) if rest.strip() else []


def _split_complete(buffer: str) -> tuple[list[str], str]:
    """Return (complete sentences, remainder) for a growing buffer."""
    done: list[str] = []
    cut = 0
    if "\n" in buffer:
        cut = buffer.rfind("\n") + 1
        for line in buffer[:cut].splitlines():
            done.extend(split_sentences(line))
    rest = buffer[cut:]
    end = max((m.end() for m in _BOUNDARY.finditer(rest)), default=0)
    if end:
        done.extend(split_sentences(rest[:end]))
        rest = rest[end:]
    return done, rest
