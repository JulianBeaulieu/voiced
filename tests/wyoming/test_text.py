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


def test_accumulator_keeps_abbreviations_and_numbers_with_their_sentence() -> None:
    """The streaming split follows voiced.text.split_sentences: "Dr." does not
    end a sentence, so "Call Dr." is not released before "Smith" arrives, and
    an integer before a period does not either (it may be an ordinal). For
    raw English that only makes the first chunk longer: normalize spells the
    digits before the engine splits again (ADR 0023)."""
    acc = SentenceAccumulator()
    assert acc.add("Call Dr. ") == []
    assert acc.add("Smith at 3. ") == []
    assert acc.add("Then rest. ") == []
    assert acc.add("Bye.") == ["Call Dr. Smith at 3. Then rest."]
    assert acc.finish() == ["Bye."]


def test_accumulator_keeps_the_raw_tail_between_chunks() -> None:
    acc = SentenceAccumulator()
    assert acc.add("Hello there. How ") == ["Hello there."]
    assert acc.add("are you? Fine") == ["How are you?"]
    assert acc.finish() == ["Fine"]


def test_accumulator_releases_cjk_sentences_without_spaces() -> None:
    excl, quest = "\uff01", "\uff1f"  # fullwidth
    acc = SentenceAccumulator()
    assert acc.add("下雨了。我们") == ["下雨了。"]
    assert acc.add(f"走吧{excl}你来吗{quest}") == [f"我们走吧{excl}"]
    assert acc.finish() == [f"你来吗{quest}"]
