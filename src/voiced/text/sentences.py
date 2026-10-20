"""Sentence splitting for streaming synthesis.

Engines synthesize one sentence at a time so the first audio arrives early.
The splitter is deliberately conservative: a false split in the middle of a
sentence sounds worse than two sentences spoken back to back. It knows about
common abbreviations, decimals, quotes and closing brackets, and falls back to
clause and word boundaries for sentences longer than ``max_len`` characters so
no chunk ever exceeds what the models accept.

The terminators and the abbreviation list are the same in every language:
in another language an English abbreviation can only keep two sentences
together, the cheaper mistake. Only whether a number followed by a period
ends a sentence depends on the language (ADR 0023).

Pure Python, no model, no dependencies.
"""

from __future__ import annotations

import re

from voiced.text.language import primary_subtag

__all__ = [
    "CLOSERS",
    "DEFAULT_MAX_LEN",
    "SPACED_TERMINATORS",
    "UNSPACED_TERMINATORS",
    "cut_to_length",
    "split_sentences",
]

DEFAULT_MAX_LEN = 300
"""Longest chunk returned, in characters. Kokoro accepts 510 phonemes and a
phoneme string is at most about 1.2 times the length of its text."""

# A token followed by a period that does not end a sentence. Compared in
# lowercase without the period. ``normalize`` expands a few of these ("e.g.",
# "etc.", "vs.") before text gets here; the list is kept complete so the
# splitter also works on text that was not normalized.
_ABBREVIATIONS = frozenset(
    {
        "mr",
        "mrs",
        "ms",
        "dr",
        "prof",
        "sr",
        "jr",
        "st",
        "mt",
        "ft",
        "vs",
        "etc",
        "e.g",
        "i.e",
        "cf",
        "inc",
        "ltd",
        "co",
        "corp",
        "no",
        "fig",
        "approx",
        "dept",
        "est",
        "jan",
        "feb",
        "mar",
        "apr",
        "jun",
        "jul",
        "aug",
        "sep",
        "sept",
        "oct",
        "nov",
        "dec",
        "mon",
        "tue",
        "wed",
        "thu",
        "fri",
        "sat",
        "sun",
        "a.m",
        "p.m",
        "u.s",
        "u.k",
        "e.u",
    }
)

SPACED_TERMINATORS = ".!?\u2026\u061f\u0964\u0965"
"""End a sentence when whitespace follows: the Latin marks, the ellipsis, the
Arabic question mark and the Devanagari danda and double danda."""

UNSPACED_TERMINATORS = "\u3002\uff01\uff1f"
"""End a sentence with or without whitespace after, as CJK is written: the
ideographic full stop and the fullwidth exclamation and question marks."""

CLOSERS = "\"')]\u201d\u2019\u300d\u300f\uff09\u300b\u3011\u00bb"
"""Closing quotes and brackets that belong to the sentence before them."""

# Languages in which a number followed by a period and a capitalised word ends
# the sentence ("It costs 5. Then"). Elsewhere it is often an ordinal ("am 3.
# Oktober"), so it never ends one.
_NUMBER_FINAL_LANGUAGES = frozenset({"en"})

# Candidate boundary: a terminator, closing quotes or brackets, and the
# whitespace after. Group 1 is everything up to the whitespace. A match
# starts only at the first terminator of a run and nothing in it backtracks,
# so a long run of "!" costs linear time.
_SPACED = re.escape(SPACED_TERMINATORS)
_UNSPACED = re.escape(UNSPACED_TERMINATORS)
_CLOSER = f"[{re.escape(CLOSERS)}]*+"
_BOUNDARY = re.compile(
    rf"(?<![{_SPACED}{_UNSPACED}])"
    rf"([{_SPACED}]++{_CLOSER}(?=\s)|[{_UNSPACED}]++{_CLOSER})\s*"
)

# Where to cut an over-long sentence, in order of preference.
_CLAUSE_BREAK = re.compile(r"[,;:\u2014]\s+|\s+-\s+|\s+\u2013\s+")
_WORD_BREAK = re.compile(r"\s+")


def split_sentences(text: str, *, language: str, max_len: int = DEFAULT_MAX_LEN) -> list[str]:
    """Split ``text`` into sentences of at most ``max_len`` characters.

    ``language`` is the BCP-47 tag of the voice that will speak the text;
    ``max_len`` is keyword-only so callers never depend on its position.

    Paragraph breaks (newlines) always split. Inside a paragraph, a sentence
    ends at one of :data:`UNSPACED_TERMINATORS`, or at one of
    :data:`SPACED_TERMINATORS` followed by whitespace, unless the period
    belongs to a known abbreviation, an initial (``J. R. R.``) or a number
    (``3.14``, ``1. item``; outside English, any number). Closing quotes and
    brackets stay with the sentence they close. An ellipsis only ends a
    sentence when the next word is capitalised. Sentences longer than
    ``max_len`` are cut at the last clause boundary before the limit, then at
    the last space, then hard.

    Returns stripped, non-empty strings; an empty or blank input gives ``[]``.
    """
    if max_len < 1:
        raise ValueError("max_len must be >= 1")
    numbers_end = primary_subtag(language) in _NUMBER_FINAL_LANGUAGES
    out: list[str] = []
    for paragraph in re.split(r"\s*\n+\s*", text):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        for sentence in _split_paragraph(paragraph, numbers_end):
            out.extend(cut_to_length(sentence, max_len))
    return out


def _split_paragraph(paragraph: str, numbers_end: bool) -> list[str]:
    sentences: list[str] = []
    start = 0
    for m in _BOUNDARY.finditer(paragraph):
        if not _is_boundary(paragraph, m, start, numbers_end):
            continue
        sentences.append(paragraph[start : m.end(1)].strip())
        start = m.end()
    tail = paragraph[start:].strip()
    if tail:
        sentences.append(tail)
    return [s for s in sentences if s]


def _is_boundary(text: str, m: re.Match[str], sentence_start: int, numbers_end: bool) -> bool:
    punct = m.group(1)
    before = text[: m.start()]
    after = text[m.end() :]
    if punct.startswith("\u2026") or punct.startswith("..."):
        # "wait... what" is one sentence; "Done... Next thing" is two.
        return after[:1].isupper() or after[:1] in "\"'\u201c\u2018(["
    if punct[0] != ".":
        return True
    word = _last_word(before)
    if not word:
        return True
    # Initials and single letters: "J. R. R. Tolkien", "vitamin C. Next"
    # is ambiguous; keep initials together only when the letter is uppercase
    # and the following word is also an initial or a capitalised name.
    if len(word) == 1 and word.isalpha() and word.isupper():
        nxt = _first_word(after)
        return not (nxt and nxt[:1].isupper())
    lowered = word.lower()
    if lowered in _ABBREVIATIONS:
        # "etc." and "vs." can end a sentence when the next word is capitalised
        # and the abbreviation is one that normally ends a clause.
        if lowered in {"etc", "no"} and after[:1].isupper():
            return True
        return False
    # Numbered list markers and section numbers at the start of a sentence:
    # "1. Apples", "2.3. Title". Elsewhere a number ends the sentence only
    # where it cannot be an ordinal: "It costs 5. Then" but "am 3. Oktober".
    if word.replace(".", "").isdigit() and after[:1].isupper():
        return numbers_end and before[sentence_start:].strip() != word
    return True


def _last_word(text: str) -> str:
    stripped = text.rstrip(CLOSERS)
    i = len(stripped)
    while i > 0 and not stripped[i - 1].isspace() and stripped[i - 1] not in "\"'\u201c\u2018([":
        i -= 1
    return stripped[i:]


def _first_word(text: str) -> str:
    return re.split(r"\s", text.lstrip("\"'\u201c\u2018(["), maxsplit=1)[0] if text else ""


def cut_to_length(text: str, max_len: int) -> list[str]:
    """Cut ``text`` into pieces of at most ``max_len`` characters: at the last
    clause boundary before the limit, else the last space, else hard. Used
    for over-long sentences here and for over-long phoneme strings by the
    Kokoro engine."""
    if max_len < 1:
        raise ValueError("max_len must be >= 1")
    parts: list[str] = []
    rest = text.strip()
    while len(rest) > max_len:
        cut = _best_cut(rest, max_len)
        head, rest = rest[:cut].rstrip(), rest[cut:].lstrip()
        if head:
            parts.append(head)
        if not rest:
            break
    if rest:
        parts.append(rest)
    return parts


def _best_cut(text: str, max_len: int) -> int:
    """Index to cut ``text`` at so the stripped head is at most ``max_len``
    long: after the last clause punctuation that fits, else at the last space
    that fits, else at ``max_len``."""
    window = text[: max_len + 1]
    for pattern in (_CLAUSE_BREAK, _WORD_BREAK):
        best: int | None = None
        for m in pattern.finditer(window):
            # A clause cut keeps the punctuation; a word cut drops the space.
            head_end = m.end() if pattern is _CLAUSE_BREAK else m.start()
            if 0 < len(window[:head_end].rstrip()) <= max_len:
                best = head_end
        if best is not None:
            return best
    return max_len
