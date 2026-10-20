"""``scripts/bench_engine.py``: WER, results and the generated document (ADR 0024).

The harness itself needs models and runs by hand; these check what decides
the numbers, how results are kept, and that docs/benchmarks.md is what its
results render to. The rating limits are tested in tests/core/test_ratings.py.
"""

from __future__ import annotations

import dataclasses
import hashlib
import io
import json
import subprocess
import tarfile
from pathlib import Path
from types import ModuleType
from typing import Any

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
        ("Mr. and Mrs. Dr. Smith", ["mister", "and", "missus", "doctor", "smith"]),
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


def test_wer_without_reference_words_is_refused(be: ModuleType) -> None:
    with pytest.raises(ValueError, match="no reference words"):
        be.wer_percent([("?!", "hello")])


# -- the clip set ------------------------------------------------------------


def test_the_clip_set_is_fixed(be: ModuleType) -> None:
    ids = [uid for uid, _, _ in be.CLIPS]
    assert len(ids) == 24 and len(set(ids)) == 24
    assert all(len(a) == len(b) == 64 for _, a, b in be.CLIPS)
    assert len({uid.split("-")[0] for uid in ids}) == 8  # speakers


@pytest.mark.parametrize(
    ("text", "number"),
    [
        ("THIS WAS AT THE MARCH ELECTION EIGHTEEN FIFTY FIVE", True),
        ("SATURDAY AUGUST FIFTEENTH", True),
        ("A BOY OF SEVENTEEN", True),
        ("THE BOGUS LEGISLATURE NUMBERED THIRTY SIX MEMBERS", True),
        ("NO ONE WAS THERE AT FIRST", False),
        ("THE SECOND DOOR STOOD OPEN", False),
    ],
)
def test_number_words(be: ModuleType, text: str, number: bool) -> None:
    assert bool(be.NUMBER_WORDS.search(text)) is number


def _archive(members: dict[str, bytes]) -> io.BytesIO:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    buffer.seek(0)
    return buffer


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_extract_keeps_only_the_clip_set_and_stops_when_complete(
    be: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    flac, text = b"fLaC-a", b"HELLO THERE"
    monkeypatch.setattr(be, "CLIPS", (("1-2-0001", _sha(flac), _sha(text)),))
    archive = _archive(
        {
            "LibriSpeech/test-clean/1/2/1-2-0000.flac": b"other",
            "LibriSpeech/test-clean/1/2/1-2-0001.flac": flac,
            "LibriSpeech/test-clean/1/2/1-2.trans.txt": b"1-2-0000 NOT\n1-2-0001 HELLO THERE\n",
            "LibriSpeech/test-clean/9/9/9-9-0000.flac": b"never read",
        }
    )
    be.extract_clips(archive, tmp_path)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["1-2-0001.flac", "1-2-0001.txt"]
    assert (tmp_path / "1-2-0001.txt").read_bytes() == text


def test_extract_refuses_different_bytes(
    be: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(be, "CLIPS", (("1-2-0001", _sha(b"expected"), _sha(b"T")),))
    archive = _archive({"x/1-2-0001.flac": b"changed upstream"})
    with pytest.raises(SystemExit, match="does not match"):
        be.extract_clips(archive, tmp_path)


def test_extract_says_what_the_archive_lacks(
    be: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(be, "CLIPS", (("1-2-0001", _sha(b"f"), _sha(b"T")),))
    with pytest.raises(SystemExit, match="lacks"):
        be.extract_clips(_archive({"x/1-2-0001.flac": b"f"}), tmp_path)


# -- results -----------------------------------------------------------------


def stt(be: ModuleType, **changes: object) -> Any:
    metrics = be.STTMetrics(
        wer_percent=2.0,
        clips=24,
        batch_rtf=0.02,
        finalize_after_silence=be.Latency(1.0, 16.0, 64),
        finalize_at_last_word=be.Latency(114.0, 165.0, 64),
    )
    return dataclasses.replace(metrics, **changes)


def tts(be: ModuleType, **changes: object) -> Any:
    metrics = be.TTSMetrics(
        voice="af_heart",
        utmos=4.4,
        first_audio=be.Latency(96.0, 112.0, 10),
        first_audio_while_stt_streams=be.Latency(140.0, 350.0, 30),
        rtf=0.05,
    )
    return dataclasses.replace(metrics, **changes)


def result(be: ModuleType, kind: str = "stt", **changes: object) -> Any:
    base = be.Result(
        kind=kind,
        engine="parakeet" if kind == "stt" else "kokoro",
        model="mlx-community/parakeet-tdt-0.6b-v3" if kind == "stt" else "mlx-community/K-82M",
        hardware="MacBook Pro, Apple M1 Max, 64 GB",
        macos="15.7.4",
        commit="a9b3c96+",
        version="0.1.1",
        date="2026-10-21",
        languages=["en-US", "en-GB"],
        licence="CC-BY-4.0",
        size_bytes=2_508_532_829,
        load_s=3.2,
        footprint_bytes=1_210_000_000,
        mlx_load_peak_bytes=3_600_000_000,
        mlx_peak_bytes=3_600_000_000,
        metrics=stt(be) if kind == "stt" else tts(be),
    )
    return dataclasses.replace(base, **changes)


@pytest.mark.parametrize("kind", ["stt", "tts"])
def test_results_round_trip_through_json(be: ModuleType, kind: str) -> None:
    r = result(be, kind)
    assert be.Result.from_json(r.to_json()) == r


def test_an_older_file_without_a_newer_field_still_loads(be: ModuleType) -> None:
    """A field added later takes its default, so files already committed
    stay valid; a field the harness does not know is ignored."""

    @dataclasses.dataclass(frozen=True)
    class Grown:
        median_ms: float
        worst_ms: float
        n: int
        p95_ms: float | None = None

    assert be.decode(Grown, {"median_ms": 1.0, "worst_ms": 2.0, "n": 3}) == Grown(1.0, 2.0, 3)
    data = json.loads(result(be).to_json())
    data["from_a_newer_harness"] = True
    assert be.Result.from_json(json.dumps(data)) == result(be)


def test_only_complete_runs_are_saved(
    be: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(be, "RESULTS_DIR", tmp_path)
    with pytest.raises(SystemExit, match="partial"):
        be.save(result(be, load_s=None))
    with pytest.raises(SystemExit, match="partial"):
        be.save(result(be, "tts", metrics=tts(be, utmos=None)))
    path = be.save(result(be))
    assert be.Result.from_json(path.read_text()) == result(be)


def test_save_replaces_the_same_model_on_the_same_hardware(
    be: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(be, "RESULTS_DIR", tmp_path)
    be.save(result(be, macos="15.7.4"))
    be.save(result(be, macos="26.1", date="2026-11-01"))
    (only,) = tmp_path.glob("*.json")
    assert be.Result.from_json(only.read_text()).macos == "26.1"
    assert only.name == (
        "stt-parakeet-mlx-community-parakeet-tdt-0-6b-v3-macbook-pro-apple-m1-max-64-gb.json"
    )


def test_save_refuses_a_file_that_holds_another_model(
    be: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(be, "RESULTS_DIR", tmp_path)
    be.save(result(be, model="org/a.b"))
    with pytest.raises(SystemExit, match="holds"):
        be.save(result(be, model="org/a-b"))  # the same slug


# -- the document ------------------------------------------------------------


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
        "1.21 GB",
        "3.60 GB / 3.60 GB",
        "2.5 GB",
        "en-US, en-GB",
        "CC-BY-4.0",
        "MacBook Pro, Apple M1 Max, 64 GB, macOS 15.7.4",
        "0.1.1 (a9b3c96+), 2026-10-21",
    ]
    assert len(cells) == len(be.STT_HEADERS)


def test_tts_row(be: ModuleType) -> None:
    r = result(
        be,
        "tts",
        mlx_load_peak_bytes=None,
        mlx_peak_bytes=None,
        size_bytes=None,
        languages=[f"l{i}" for i in range(9)],
    )
    cells = be.row(r)
    assert len(cells) == len(be.TTS_HEADERS)
    assert cells[2:9] == [
        "5 of 5",
        "5 of 5",
        "4.40",
        "96 / 112 ms",
        "140 / 350 ms",
        "0.050",
        "`af_heart`",
    ]
    assert cells[9:14] == ["3.2 s", "1.21 GB", "none / none", "not cached", "9 languages"]


def test_render_has_one_table_per_kind_in_a_stable_order(be: ModuleType) -> None:
    rows = [result(be, "tts"), result(be, engine="zeta"), result(be)]
    doc = be.render(rows)
    assert doc.startswith(be.DOC_HEADER)
    stt_part = doc.split("## Speech to text")[1].split("## Text to speech")[0]
    tts_part = doc.split("## Text to speech")[1].split("## Ratings")[0]
    assert stt_part.index("| parakeet |") < stt_part.index("| zeta |")
    assert "kokoro" not in stt_part and "| kokoro |" in tts_part
    assert be.render(list(reversed(rows))) == doc


def test_render_without_results(be: ModuleType) -> None:
    assert be.render([]).count("No results yet.") == 2


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


# -- the harness's edges -----------------------------------------------------


def _completed(stdout: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], 0, stdout=stdout, stderr="")


@pytest.mark.parametrize(
    ("stdout", "score"),
    [
        ("downloading...\n[4.5, 4.4]\n", 4.45),
        ("", None),
        ("not json\n", None),
        ("[4.5]\n", None),  # one score for two files
    ],
)
def test_utmos_parses_the_last_line_or_says_why_not(
    be: ModuleType, monkeypatch: pytest.MonkeyPatch, stdout: str, score: float | None
) -> None:
    monkeypatch.setattr(be.subprocess, "run", lambda *a, **k: _completed(stdout))
    assert be.utmos([Path("a.wav"), Path("b.wav")]) == score


def test_utmos_failure_to_run_is_none(be: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*args: object, **kwargs: object) -> None:
        raise subprocess.CalledProcessError(1, "uv", stderr="no network")

    monkeypatch.setattr(be.subprocess, "run", fail)
    assert be.utmos([Path("a.wav")]) is None


class FakeProcess:
    def __init__(self) -> None:
        self.terminated = False

    def poll(self) -> int | None:
        return None

    def terminate(self) -> None:
        self.terminated = True

    def wait(self, timeout: float | None = None) -> int:
        return 0


def test_started_stops_the_service_after_use_and_on_error(
    be: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    procs: list[FakeProcess] = []
    envs: list[dict[str, str]] = []

    def popen(argv: list[str], env: dict[str, str], **kwargs: object) -> FakeProcess:
        envs.append(env)
        procs.append(FakeProcess())
        return procs[-1]

    monkeypatch.setenv("VOICED_SERVER__API_KEY", "from-the-shell")
    monkeypatch.setattr(be.subprocess, "Popen", popen)
    monkeypatch.setattr(be, "_ready", lambda url: True)
    monkeypatch.setattr(be.bench, "engine_name", lambda url: "parakeet")
    with be.started("stt", "parakeet", "m", tmp_path) as server:
        assert server.url.startswith("http://127.0.0.1:")
    with pytest.raises(RuntimeError), be.started("stt", "parakeet", "m", tmp_path):
        raise RuntimeError
    with pytest.raises(SystemExit, match="runs parakeet, not kokoro"):
        with be.started("stt", "kokoro", "m", tmp_path):
            pass
    assert [p.terminated for p in procs] == [True, True, True]
    assert "VOICED_SERVER__API_KEY" not in envs[0]
    assert envs[0]["VOICED_STT__ENGINE"] == "parakeet"
    assert "hf_home" in Path(envs[0]["VOICED_CONFIG"]).read_text()


def test_free_ports_are_distinct(be: ModuleType) -> None:
    ports = be._free_ports(4)
    assert len(set(ports)) == 4
