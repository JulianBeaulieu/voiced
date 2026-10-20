"""``scripts/bench_engine.py``: WER, ratings and the generated document (ADR 0023).

The harness itself needs models and runs by hand; these check what decides
the numbers and the ratings, and that docs/benchmarks.md is what its
results render to.
"""

from __future__ import annotations

import dataclasses
from types import ModuleType

import pytest

from tests.scripts_support import load


@pytest.fixture(scope="module")
def be() -> ModuleType:
    return load("bench_engine")


# -- normalizer and WER ------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "words"),
    [
        ("CONCORD RETURNED TO ITS PLACE", ["concord", "returned", "to", "its", "place"]),
        ("Concord returned, to its place.", ["concord", "returned", "to", "its", "place"]),
        ("That summer's emigration", ["that", "summer's", "emigration"]),
        ("That summer\u2019s emigration", ["that", "summer's", "emigration"]),
        ("'Who taught you?' she asked", ["who", "taught", "you", "she", "asked"]),
        ("well-known \u2014 to-day", ["well", "known", "to", "day"]),
        ("The Tim\u00e6us, caf\u00e9", ["the", "timaeus", "cafe"]),
        ("  spaced\tout\nwords  ", ["spaced", "out", "words"]),
        ("", []),
        ("?!", []),
    ],
)
def test_normalize(be: ModuleType, text: str, words: list[str]) -> None:
    assert be.normalize(text) == words


@pytest.mark.parametrize(
    ("reference", "hypothesis", "errors"),
    [
        ("a b c", "a b c", 0),
        ("a b c", "a x c", 1),  # substitution
        ("a b c", "a c", 1),  # deletion
        ("a b c", "a b b c", 1),  # insertion
        ("a b c", "", 3),
        ("", "a b", 2),
        ("the cat sat", "cat sat on", 2),
    ],
)
def test_word_errors(be: ModuleType, reference: str, hypothesis: str, errors: int) -> None:
    assert be.word_errors(reference.split(), hypothesis.split()) == errors


def test_wer_is_over_every_reference_word(be: ModuleType) -> None:
    """Corpus WER: 1 error in 4 words plus 0 in 6 is 10 %, not the 12.5 %
    mean of the two clips' rates."""
    pairs = [("one two three four", "one two three for"), ("A b c d e f.", "a b c d e f")]
    assert be.wer_percent(pairs) == pytest.approx(10.0)


def test_wer_ignores_case_and_punctuation(be: ModuleType) -> None:
    assert be.wer_percent([("WHO TAUGHT YOU", "Who taught you?")]) == 0.0


def test_the_clip_set_is_fixed(be: ModuleType) -> None:
    ids = [uid for uid, _ in be.CLIPS]
    assert len(ids) == 24 and len(set(ids)) == 24
    assert all(len(sha) == 64 and int(sha, 16) >= 0 for _, sha in be.CLIPS)
    assert len({uid.split("-")[0] for uid in ids}) == 8  # speakers


# -- ratings -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "rating"),
    [(0.0, 5), (3.0, 5), (3.01, 4), (5.0, 4), (8.0, 3), (12.0, 2), (12.01, 1), (90.0, 1)],
)
def test_lower_is_better_scale(be: ModuleType, value: float, rating: int) -> None:
    assert be.STT_WER.rate(value) == rating


@pytest.mark.parametrize(
    ("value", "rating"),
    [(4.5, 5), (4.3, 5), (4.29, 4), (4.0, 4), (3.6, 3), (3.0, 2), (2.99, 1)],
)
def test_higher_is_better_scale(be: ModuleType, value: float, rating: int) -> None:
    assert be.TTS_UTMOS.rate(value) == rating


def test_scales_are_monotonic(be: ModuleType) -> None:
    for _, scale in be.RATED:
        bounds = list(scale.bounds)
        assert bounds == sorted(bounds, reverse=scale.higher_is_better), scale.measure


def stt(be: ModuleType, **changes: object) -> object:
    metrics = be.STTMetrics(
        wer_percent=2.0,
        clips=24,
        batch_rtf=0.02,
        finalize_after_silence=be.Latency(1.0, 16.0, 32),
        finalize_at_last_word=be.Latency(114.0, 165.0, 32),
    )
    return dataclasses.replace(metrics, **changes)


def tts(be: ModuleType, **changes: object) -> object:
    metrics = be.TTSMetrics(
        voice="af_heart",
        utmos=4.4,
        first_audio=be.Latency(96.0, 112.0, 10),
        first_audio_while_stt_streams=be.Latency(140.0, 350.0, 30),
        rtf=0.05,
    )
    return dataclasses.replace(metrics, **changes)


def test_stt_speed_is_the_slower_of_finalize_and_throughput(be: ModuleType) -> None:
    assert be.rate(stt(be)) == be.Rating(quality=5, speed=5)
    slow_finalize = stt(be, finalize_after_silence=be.Latency(40.0, 200.0, 32))
    assert be.rate(slow_finalize).speed == 3
    slow_batch = stt(be, batch_rtf=0.3)
    assert be.rate(slow_batch).speed == 2


def test_stt_without_streaming_is_rated_on_throughput(be: ModuleType) -> None:
    batch_only = stt(be, finalize_after_silence=None, finalize_at_last_word=None, batch_rtf=0.08)
    assert be.rate(batch_only).speed == 4


def test_tts_ratings(be: ModuleType) -> None:
    assert be.rate(tts(be)) == be.Rating(quality=5, speed=5)
    assert be.rate(tts(be, rtf=1.2)).speed == 1  # cannot keep up with playback
    assert be.rate(tts(be, first_audio=be.Latency(250.0, 300.0, 10))).speed == 3
    assert be.rate(tts(be, utmos=None)).quality is None


# -- results and the document ------------------------------------------------


def result(be: ModuleType, kind: str = "stt", **changes: object) -> object:
    base = be.Result(
        kind=kind,
        engine="parakeet" if kind == "stt" else "kokoro",
        model="mlx-community/parakeet-tdt-0.6b-v3" if kind == "stt" else "mlx-community/K-82M",
        machine="MacBook Pro, Apple M1 Max, 64 GB, macOS 15.7.4",
        commit="661789f",
        date="2026-10-20",
        languages=["en-US", "en-GB"],
        licence="CC-BY-4.0",
        size_bytes=2_508_532_829,
        load_s=3.2,
        footprint_bytes=1_210_000_000,
        mlx_peak_bytes=2_460_000_000,
        metrics=stt(be) if kind == "stt" else tts(be),
    )
    return dataclasses.replace(base, **changes)


@pytest.mark.parametrize("kind", ["stt", "tts"])
def test_results_round_trip_through_json(be: ModuleType, kind: str) -> None:
    r = result(be, kind)
    assert be.Result.from_json(r.to_json()) == r


def test_round_trip_keeps_missing_measurements(be: ModuleType) -> None:
    r = result(
        be,
        load_s=None,
        mlx_peak_bytes=None,
        metrics=stt(be, finalize_after_silence=None, finalize_at_last_word=None),
    )
    assert be.Result.from_json(r.to_json()) == r


def test_filename_is_per_model_and_hardware_not_os(be: ModuleType) -> None:
    a = result(be)
    b = result(be, machine="MacBook Pro, Apple M1 Max, 64 GB, macOS 26.1")
    assert a.filename() == b.filename()
    assert a.filename() == (
        "stt-parakeet-mlx-community-parakeet-tdt-0-6b-v3-macbook-pro-apple-m1-max-64-gb.json"
    )


def test_stt_row(be: ModuleType) -> None:
    cells = be.row(result(be))
    assert cells == [
        "`mlx-community/parakeet-tdt-0.6b-v3`",
        "parakeet",
        "5 of 5",
        "5 of 5",
        "2.0%",
        "1 / 16 ms",
        "114 / 165 ms",
        "0.020",
        "3.2 s",
        "1.21 GB / 2.46 GB",
        "2.5 GB",
        "en-US, en-GB",
        "CC-BY-4.0",
        "MacBook Pro, Apple M1 Max, 64 GB, macOS 15.7.4",
        "661789f, 2026-10-20",
    ]
    assert len(cells) == len(be.STT_HEADERS)


def test_tts_row_marks_what_was_not_measured(be: ModuleType) -> None:
    r = result(
        be,
        "tts",
        load_s=None,
        mlx_peak_bytes=None,
        size_bytes=None,
        languages=[f"l{i}" for i in range(9)],
        metrics=tts(be, utmos=None, first_audio_while_stt_streams=None),
    )
    cells = be.row(r)
    assert len(cells) == len(be.TTS_HEADERS)
    assert cells[2:9] == ["not rated", "5 of 5", "n/a", "96 / 112 ms", "n/a", "0.050", "`af_heart`"]
    assert cells[9:13] == ["not measured", "1.21 GB / none", "not cached", "9 languages"]


def test_render_has_one_table_per_kind_in_a_stable_order(be: ModuleType) -> None:
    rows = [
        result(be, "tts"),
        result(be, engine="zeta"),
        result(be),
    ]
    doc = be.render(rows)
    assert doc.startswith(be.DOC_HEADER)
    stt_part = doc.split("## Speech to text")[1].split("## Text to speech")[0]
    tts_part = doc.split("## Text to speech")[1].split("## Ratings")[0]
    assert stt_part.index("| parakeet |") < stt_part.index("| zeta |")
    assert "kokoro" not in stt_part and "| kokoro |" in tts_part
    assert be.render(list(reversed(rows))) == doc


def test_render_without_results(be: ModuleType) -> None:
    doc = be.render([])
    assert doc.count("No results yet.") == 2


def test_rating_limits_in_the_document_come_from_the_scales(be: ModuleType) -> None:
    doc = be.render([])
    wer = "| STT quality | word error rate on the clip set | <= 3% | <= 5% | <= 8% | <= 12% |"
    assert wer + " otherwise |" in doc
    assert "| TTS quality | UTMOS naturalness | >= 4.3 | >= 4 | >= 3.6 | >= 3 | otherwise |" in doc


def test_the_document_is_the_rendered_results(be: ModuleType) -> None:
    """docs/benchmarks.md is generated: regenerate it with
    ``uv run python scripts/bench_engine.py render``."""
    expected = be.render(be.load_results(be.RESULTS_DIR))
    assert be.DOC.read_text() == expected, "run: uv run python scripts/bench_engine.py render"
