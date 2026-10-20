"""Streaming sentence accumulation for the TTS listener.

:func:`voiced.text.split_sentences` owns the sentence rule (abbreviations,
initials, numbers, ellipses); this module only adds the incremental buffering
the Wyoming streaming path needs, handing out each sentence as soon as the
text after it shows it is complete, so synthesis can start before the
conversation agent has finished writing.
"""

from __future__ import annotations

from voiced.text import split_sentences


class SentenceAccumulator:
    """Collects streamed text and hands out sentences as soon as they are complete.

    ``add`` returns the sentences that became complete with this chunk: every
    sentence ``split_sentences`` finds in the buffer except the last, which
    may still be growing ("Call Dr." becomes "Call Dr. Smith"). The last one is
    released early only when a newline follows it. ``finish`` returns whatever
    is left when the stream ends.
    """

    def __init__(self, language: str) -> None:
        self._language = language
        self._buffer = ""

    def add(self, text: str) -> list[str]:
        self._buffer += text
        parts = split_sentences(self._buffer, language=self._language)
        if not parts:
            return []
        last = parts[-1]
        start = self._buffer.rfind(last)
        if "\n" in self._buffer[start + len(last) :]:
            self._buffer = ""
            return parts
        # Keep the raw tail, including its trailing whitespace, so the next
        # chunk joins it exactly as the agent wrote it.
        self._buffer = self._buffer[start:]
        return parts[:-1]

    def finish(self) -> list[str]:
        rest, self._buffer = self._buffer, ""
        return split_sentences(rest, language=self._language)
