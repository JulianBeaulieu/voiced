"""``voiced.ratings``: the fixed limits ADR 0024 publishes."""

from __future__ import annotations

import math

import pytest

from voiced import ratings
from voiced.ratings import (
    STT_FINALIZE,
    STT_RTF,
    STT_WER,
    TTS_FIRST_AUDIO,
    TTS_RTF,
    TTS_UTMOS,
    Rating,
    rate,
)


def test_the_limits_are_the_adr_table() -> None:
    """ADR 0024 section 4. Changing a limit changes every rating: change the
    ADR with it."""
    table = {(s.kind, s.axis, s.measure): (s.bounds, s.higher_is_better) for s in ratings.SCALES}
    assert table == {
        ("stt", "quality", "word error rate on the clip set"): ((3, 5, 8, 12), False),
        ("stt", "speed", "finalize after 200 ms of silence, worst"): ((50, 150, 300, 600), False),
        ("stt", "speed", "batch real-time factor"): ((0.05, 0.1, 0.2, 0.5), False),
        ("tts", "quality", "UTMOS naturalness"): ((4.3, 4.0, 3.6, 3.0), True),
        ("tts", "speed", "first audio, median"): ((100, 200, 400, 800), False),
        ("tts", "speed", "real-time factor"): ((0.1, 0.25, 0.5, 1.0), False),
    }


@pytest.mark.parametrize(
    ("value", "rating"),
    [(0.0, 5), (3.0, 5), (3.01, 4), (5.0, 4), (8.0, 3), (12.0, 2), (12.01, 1), (90.0, 1)],
)
def test_lower_is_better_scale(value: float, rating: int) -> None:
    assert ratings.STT_WER.rate(value) == rating


@pytest.mark.parametrize(
    ("value", "rating"),
    [(4.5, 5), (4.3, 5), (4.29, 4), (4.0, 4), (3.6, 3), (3.0, 2), (2.99, 1)],
)
def test_higher_is_better_scale(value: float, rating: int) -> None:
    assert ratings.TTS_UTMOS.rate(value) == rating


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_a_value_that_is_not_a_number_is_refused(value: float) -> None:
    with pytest.raises(ValueError, match="not a measurement"):
        ratings.STT_RTF.rate(value)


def stt(wer: float, finalize: float, rtf: float) -> Rating:
    return rate([(STT_WER, wer), (STT_FINALIZE, finalize), (STT_RTF, rtf)])


def test_stt_speed_is_the_slower_of_finalize_and_throughput() -> None:
    assert stt(2.0, 16.0, 0.02) == Rating(quality=5, speed=5)
    assert stt(2.0, 200.0, 0.02).speed == 3
    assert stt(2.0, 16.0, 0.3).speed == 2
    assert stt(9.0, 16.0, 0.02).quality == 2


def test_tts_ratings() -> None:
    assert rate([(TTS_UTMOS, 4.4), (TTS_FIRST_AUDIO, 96.0), (TTS_RTF, 0.05)]) == Rating(5, 5)
    assert rate([(TTS_FIRST_AUDIO, 96.0), (TTS_RTF, 1.2)]) == Rating(None, 1)  # not scored
    assert rate([(TTS_FIRST_AUDIO, 250.0), (TTS_RTF, 0.05)]).speed == 3


def test_a_rating_needs_a_speed_measurement() -> None:
    with pytest.raises(ValueError, match="no speed"):
        rate([(STT_WER, 2.0)])
