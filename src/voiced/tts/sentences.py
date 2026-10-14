"""Sentence splitting for streaming synthesis.

Engines synthesize one sentence at a time so the first audio arrives early.
The splitter is deliberately conservative: a false split in the middle of a
sentence sounds worse than two sentences spoken back to back. It knows about
common abbreviations, decimals, quotes and closing brackets, and falls back to
clause and word boundaries for sentences longer than ``max_len`` characters so
no chunk ever exceeds what the models accept.

Pure Python, no model, no dependencies.
"""

from __future__ import annotations

import re

__all__ = ["DEFAULT_MAX_LEN", "split_sentences"]

DEFAULT_MAX_LEN = 300
"""Longest chunk returned, in characters. Kokoro accepts 510 phonemes and a
phoneme string is at most about 1.2 times the length of its text."""

# A token followed by a period that does not end a sentence. Compared in
# lowercase without the period.
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

# Candidate boundary: terminal punctuation, optional closing quotes or
# brackets, then whitespace. Group 1 is everything up to the whitespace.
_BOUNDARY = re.compile(r"([.!?\u2026]+[\"'\u201d\u2019)\]]*)\s+")

# Where to cut an over-long sentence, in order of preference.
_CLAUSE_BREAK = re.compile(r"[,;:\u2014]\s+|\s+-\s+|\s+\u2013\s+")
_WORD_BREAK = re.compile(r"\s+")


def split_sentences(text: str, *, max_len: int = DEFAULT_MAX_LEN) -> list[str]:
    """Split ``text`` into sentences of at most ``max_len`` characters.

    The positional signature is ``split_sentences(text) -> list[str]`` and is
    stable; ``max_len`` is keyword-only so callers never depend on its position.

    Paragraph breaks (newlines) always split. Inside a paragraph, a sentence
    ends at ``.``, ``!``, ``?`` or an ellipsis followed by whitespace, unless
    the period belongs to a known abbreviation, an initial (``J. R. R.``) or a
    number (``3.14``, ``1. item``). An ellipsis only ends a sentence when the
    next word is capitalised. Sentences longer than ``max_len`` are cut at the
    last clause boundary before the limit, then at the last space, then hard.

    Returns stripped, non-empty strings; an empty or blank input gives ``[]``.
    """
    if max_len < 1:
        raise ValueError("max_len must be >= 1")
    out: list[str] = []
    for paragraph in re.split(r"\s*\n+\s*", text):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        for sentence in _split_paragraph(paragraph):
            out.extend(_limit_length(sentence, max_len))
    return out


def _split_paragraph(paragraph: str) -> list[str]:
    sentences: list[str] = []
    start = 0
    for m in _BOUNDARY.finditer(paragraph):
        if not _is_boundary(paragraph, m, start):
            continue
        sentences.append(paragraph[start : m.end(1)].strip())
        start = m.end()
    tail = paragraph[start:].strip()
    if tail:
        sentences.append(tail)
    return [s for s in sentences if s]


def _is_boundary(text: str, m: re.Match[str], sentence_start: int) -> bool:
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
    # "1. Apples", "2.3. Title". A number that ends a sentence still splits.
    if word.replace(".", "").isdigit() and after[:1].isupper():
        return before[sentence_start:].strip() != word
    return True


def _last_word(text: str) -> str:
    stripped = text.rstrip("\"'\u201d\u2019)]")
    i = len(stripped)
    while i > 0 and not stripped[i - 1].isspace() and stripped[i - 1] not in "\"'\u201c\u2018([":
        i -= 1
    return stripped[i:]


def _first_word(text: str) -> str:
    return re.split(r"\s", text.lstrip("\"'\u201c\u2018(["), maxsplit=1)[0] if text else ""


def _limit_length(sentence: str, max_len: int) -> list[str]:
    parts: list[str] = []
    rest = sentence
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
    """Index to cut ``text`` at so the head is at most ``max_len`` long."""
    window = text[: max_len + 1]
    for pattern in (_CLAUSE_BREAK, _WORD_BREAK):
        candidates = [m for m in pattern.finditer(window) if 0 < m.start() <= max_len]
        if candidates:
            # The cut goes after the punctuation and before the whitespace,
            # or at the whitespace itself for a plain word break.
            m = candidates[-1]
            return m.end() if pattern is _CLAUSE_BREAK else m.start()
    return max_len
