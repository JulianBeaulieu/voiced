from __future__ import annotations

from voiced.wyoming.text import SentenceAccumulator


def test_accumulator_emits_only_complete_sentences() -> None:
    acc = SentenceAccumulator()
    assert acc.add("Hello th") == []
    assert acc.add("ere. How are") == ["Hello there."]
    assert acc.add(" you? I am 3.5 feet") == ["How are you?"]
    assert acc.add(" tall.") == []  # no trailing whitespace yet: might continue
    assert acc.finish() == ["I am 3.5 feet tall."]
    assert acc.finish() == []


def test_accumulator_newlines_are_boundaries() -> None:
    acc = SentenceAccumulator()
    assert acc.add("Title\n- item one\n- item two. More") == ["Title", "- item one", "- item two."]
    assert acc.finish() == ["More"]
