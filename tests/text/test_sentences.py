from __future__ import annotations

import time

import pytest

from voiced.text import split_sentences
from voiced.text.sentences import DEFAULT_MAX_LEN, cut_to_length

CASES: list[tuple[str, list[str]]] = [
    ("Hello there. How are you? I'm fine!", ["Hello there.", "How are you?", "I'm fine!"]),
    ("One sentence without a period", ["One sentence without a period"]),
    ("", []),
    ("   \n\n  ", []),
    # abbreviations
    (
        "Dr. Smith went to Washington. He arrived at 3 p.m. on Monday.",
        ["Dr. Smith went to Washington.", "He arrived at 3 p.m. on Monday."],
    ),
    (
        "Mr. and Mrs. Jones left. Prof. Lee stayed.",
        ["Mr. and Mrs. Jones left.", "Prof. Lee stayed."],
    ),
    ("The U.S. is big. Yes.", ["The U.S. is big.", "Yes."]),
    ("Apples, pears, etc. are fruit. Right.", ["Apples, pears, etc. are fruit.", "Right."]),
    ("Fruit, etc. Next topic.", ["Fruit, etc.", "Next topic."]),
    ("Try e.g. this one. Or that.", ["Try e.g. this one.", "Or that."]),
    # initials
    (
        "J. R. R. Tolkien wrote it. Then C. S. Lewis.",
        ["J. R. R. Tolkien wrote it.", "Then C. S. Lewis."],
    ),
    # decimals and numbers
    ("Version 2.0 is out. Try it.", ["Version 2.0 is out.", "Try it."]),
    # An integer may be an ordinal ("am 3. Oktober"), so it never ends a sentence.
    ("It costs 5. Then 6. Done.", ["It costs 5. Then 6. Done."]),
    ("1. First item. 2. Second item.", ["1. First item.", "2. Second item."]),
    ("Pi is 3.14159. Really.", ["Pi is 3.14159.", "Really."]),
    # quotes and brackets
    ('He said "Go now." Then he left.', ['He said "Go now."', "Then he left."]),
    ("(It worked.) Next step.", ["(It worked.)", "Next step."]),
    (
        "She asked \u201cWhy?\u201d Nobody answered.",
        ["She asked \u201cWhy?\u201d", "Nobody answered."],
    ),
    # ellipsis
    ("wait... what? Done... Next one.", ["wait... what?", "Done...", "Next one."]),
    ("Well\u2026 maybe. Fine\u2026 Go.", ["Well\u2026 maybe.", "Fine\u2026", "Go."]),
    # paragraphs
    ("Para one.\n\nPara two", ["Para one.", "Para two"]),
    ("Line one\nline two.", ["Line one", "line two."]),
    # multiple terminators
    ("Really?! Yes!! Go.", ["Really?!", "Yes!!", "Go."]),
    # lowercase after a period still splits (a period is a strong signal)
    ("it ended. then it began.", ["it ended.", "then it began."]),
]


@pytest.mark.parametrize(("text", "expected"), CASES, ids=[c[0][:40] or "empty" for c in CASES])
def test_split_sentences(text: str, expected: list[str]) -> None:
    assert split_sentences(text) == expected


def test_long_sentence_is_cut_at_clause_boundaries() -> None:
    text = ", ".join(f"clause {i}" for i in range(80)) + "."
    parts = split_sentences(text, max_len=100)
    assert all(len(p) <= 100 for p in parts)
    assert all(p.endswith((",", ".")) for p in parts)
    assert " ".join(parts) == text


def test_long_sentence_without_commas_is_cut_at_words() -> None:
    text = " ".join(["word"] * 100)
    parts = split_sentences(text, max_len=50)
    assert all(len(p) <= 50 for p in parts)
    assert all(p.startswith("word") and p.endswith("word") for p in parts)
    assert " ".join(parts) == text


def test_unbreakable_run_is_cut_hard() -> None:
    text = "x" * 700
    parts = split_sentences(text)
    assert [len(p) for p in parts] == [DEFAULT_MAX_LEN, DEFAULT_MAX_LEN, 100]
    assert "".join(parts) == text


def test_default_max_len_is_respected() -> None:
    text = "A sentence that is long enough to matter. " * 40
    assert all(len(p) <= DEFAULT_MAX_LEN for p in split_sentences(text))


def test_max_len_must_be_positive() -> None:
    with pytest.raises(ValueError):
        split_sentences("x", max_len=0)


# Fullwidth exclamation and question marks and colon.
FW_EXCL, FW_QUEST, FW_COLON = "\uff01", "\uff1f", "\uff1a"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            f"下雨了。我们走吧{FW_EXCL}你来吗{FW_QUEST}",
            ["下雨了。", f"我们走吧{FW_EXCL}", f"你来吗{FW_QUEST}"],
        ),
        ("下雨了。 我们走吧。", ["下雨了。", "我们走吧。"]),
        (
            f"他说{FW_COLON}\u201c下雨了。\u201d我们走吧。",
            [f"他说{FW_COLON}\u201c下雨了。\u201d", "我们走吧。"],
        ),
        ("「你好。」我走了。", ["「你好。」", "我走了。"]),
        # A run that mixes CJK and Latin marks is one terminator.
        (f"真的{FW_QUEST}!我不信。", [f"真的{FW_QUEST}!", "我不信。"]),
        ("هل تمطر؟ نعم، إنها تمطر.", ["هل تمطر؟", "نعم، إنها تمطر."]),
        ("«مرحبا؟» نعم.", ["«مرحبا؟»", "نعم."]),
        ("बारिश हो रही है। चलो चलते हैं।", ["बारिश हो रही है।", "चलो चलते हैं।"]),
        ("नमस्ते॥ ठीक है।", ["नमस्ते॥", "ठीक है।"]),
        (
            "Dr. Smith said hi. 小雨。Then he left. هل تمطر؟ Yes.",
            ["Dr. Smith said hi.", "小雨。", "Then he left.", "هل تمطر؟", "Yes."],
        ),
    ],
)
def test_cjk_arabic_and_devanagari_terminators(text: str, expected: list[str]) -> None:
    assert split_sentences(text) == expected


def test_an_integer_before_a_period_is_not_a_sentence_end() -> None:
    assert split_sentences("Am 3. Oktober ist Feiertag. Gut.") == [
        "Am 3. Oktober ist Feiertag.",
        "Gut.",
    ]
    # The known ambiguity: "Seite 12." could end a sentence.
    assert split_sentences("Seite 12. Dann weiter.") == ["Seite 12. Dann weiter."]
    assert split_sentences("1. Erstens. 2. Zweitens.") == ["1. Erstens.", "2. Zweitens."]
    assert split_sentences("Es sind 2.5. Dann weiter.") == ["Es sind 2.5.", "Dann weiter."]


@pytest.mark.parametrize(
    "text", ["!" * 20_000, "\u3002" * 20_000, "!)" * 10_000, "Done!" + "!" * 20_000 + " a"]
)
def test_long_runs_of_terminators_are_linear(text: str) -> None:
    # Quadratic matching took about 4 s on 20,000 "!" with nothing after them.
    start = time.perf_counter()
    split_sentences(text)
    assert time.perf_counter() - start < 2.0


def test_no_empty_chunks() -> None:
    assert split_sentences(". . . ! ? ") == [".", ".", ".", "!", "?"]
    assert all(s.strip() for s in split_sentences("a.  \n\n  b.  ."))


def test_cut_to_length_prefers_clause_then_word_then_hard() -> None:
    assert cut_to_length("", 10) == []
    assert cut_to_length("short", 10) == ["short"]
    assert cut_to_length("abc def, ghi jkl", 10) == ["abc def,", "ghi jkl"]
    assert cut_to_length("abc def ghi", 7) == ["abc def", "ghi"]
    assert cut_to_length("abcdefghij", 4) == ["abcd", "efgh", "ij"]
    assert cut_to_length("abcdefghij klm", 10) == ["abcdefghij", "klm"]
    with pytest.raises(ValueError):
        cut_to_length("x", 0)
