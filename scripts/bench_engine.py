#!/usr/bin/env python3
"""Benchmark one engine and keep docs/benchmarks.md in step with it (ADR 0024).

One command per engine kind; it starts the service itself, measures it and
rates it::

    uv run python scripts/bench_engine.py stt --engine parakeet
    uv run python scripts/bench_engine.py tts --engine kokoro [--model REPO]

A complete run prints its Markdown row and ratings, writes its result to
``docs/benchmarks/<kind>-<engine>-<model>-<hardware>.json`` (replacing an
earlier run of the same model on the same hardware) and regenerates
``docs/benchmarks.md`` from every result there. ``render`` only
regenerates; a test fails when the document and the results differ.

A partial run only prints: ``--url`` (a service someone else started, as
``scripts/bench.py`` measures; no load time, and the machine is not this
one's to name), ``--no-naturalness`` and ``--no-save``. Like ``bench.py``,
this is a standalone script, so the ``openai`` client and the measurement
code stay off the daemon's path.

What a run measures (the protocol is ADR 0024's):

* the service started twice in a row, the second start timed from spawn to
  ``/health`` answering 200 (the first downloads a missing model and warms
  the disk cache), and MLX's peak right after it;
* stt: word error rate and real-time factor of batch transcription over 24
  LibriSpeech test-clean utterances (:data:`CLIPS`, downloaded once into
  ``--cache``), and streaming finalize over ``bench.py``'s fixture at 32
  alignments each of 64 and 100 ms chunks, after 200 ms of trailing silence
  and after none (ADR 0008's method);
* tts: first audio for ``bench.py``'s sentence, alone and while a second
  client streams to an STT service (the default engine, started alongside
  unless ``--stt-url``), the real-time factor of a long reply, and the
  UTMOS naturalness of :data:`NATURALNESS_TEXTS`, scored by
  ``scripts/utmos_score.py`` in a throwaway uv environment after the
  service has stopped;
* memory from the service's ``/metrics.json`` 1 s after use: the physical
  footprint and MLX's peak; the model's size in the Hugging Face cache;
  languages and licence from the engine, built unloaded for the model.

Result files are read back field by field: a field added later gets a
default, so files written before it stay valid.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import dataclasses
import hashlib
import json
import math
import os
import re
import socket
import statistics
import subprocess
import sys
import tarfile
import tempfile
import time
import typing
import unicodedata
import urllib.request
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Literal

import bench
import httpx
import numpy as np
from numpy.typing import NDArray

from voiced import ratings
from voiced.ratings import SCALES, Rating, Scale

REPO_ROOT = Path(__file__).resolve().parent.parent
RESULTS_DIR = REPO_ROOT / "docs" / "benchmarks"
DOC = REPO_ROOT / "docs" / "benchmarks.md"
SCORER = Path(__file__).resolve().parent / "utmos_score.py"
DEFAULT_CACHE = Path.home() / ".cache" / "voiced" / "bench"

Kind = Literal["stt", "tts"]

# -- the clip set ------------------------------------------------------------

LIBRISPEECH_URL = "https://www.openslr.org/resources/12/test-clean.tar.gz"
"""LibriSpeech test-clean (Panayotov et al. 2015, CC BY 4.0). The download
stops at the last clip needed, about 80 MB into the 346 MB archive."""

CLIPS: tuple[tuple[str, str, str], ...] = (
    # Four male and four female speakers, the first eight in the archive's
    # order to balance the two; per speaker the first three utterances by id
    # of 3 to 12 s whose transcript has no number words (an engine writing
    # "36" for "thirty six" is formatting, not a recognition error).
    # 172.5 s, 443 words. The id, the sha256 of its .flac and of its
    # transcript line after the id.
    (
        "6930-75918-0000",
        "9ce35224156f071ab58eb7feb8a5ceae600f6f9f353da2a6cbf797b6b1ac8a23",
        "9c3fd9c37616a15061047594d2ccca4213721d77b81077613c9c1005bc3d0fe7",
    ),
    (
        "6930-75918-0002",
        "74ecfd628f49dca71995972c075fcc9904df804d49a4eb5b11fb712b3584f789",
        "28176eba884aa96b09ad6f37c9e31b7b272e4c37f72dfc6a9a48a11e75392742",
    ),
    (
        "6930-75918-0004",
        "e1c4755c2febfc6b77fde418821c57e2e980d84d4c79a73ff60c6e6dfc794eac",
        "06dbadf42702dd2d7e1868abafc9351edfbcd7cdd3e427a5d2adf034a396e65a",
    ),
    (
        "1320-122612-0001",
        "0a79505430aa3995a69d287ed16f8368a889b652ff6f3f4f25c371e07f7f0a20",
        "e86a85a50e18652e3e3f26303d00e614fa14b977f4239440d209e9032263d099",
    ),
    (
        "1320-122612-0002",
        "bcf8ec844960246ee63b0c22e95beef40dfc0b6e5efae89ac7a0d78e5afe0c9b",
        "f4af7ddb27b730547384d2f3f70bbcd9f2c99581f22263da5d5686e00262cd5f",
    ),
    (
        "1320-122612-0003",
        "0c77177884f8558fe30c0c61ab80c1edd164f9a914fdeb0a60ccb7eaecb4b933",
        "396871707c2dec157e13ec460c7d6c4434957c549c337167358fab86eecfc129",
    ),
    (
        "260-123286-0001",
        "482a025752a99bedf0d539c055c80c1f0396f3f0b0094fd1d3ff89b3d26beb9e",
        "3b4b0e74e59c071eb19821c7e87e6e92592c551ea91817db562886a3363a23f0",
    ),
    (
        "260-123286-0002",
        "25d5b388fb4d8f14752fbbfb9f1b020b4de121b9be7d20ee6632b060ab88bd82",
        "9c3041d449be265fea6525f466d61239999f7c4026435a789c640a8a26d1ffeb",
    ),
    (
        "260-123286-0003",
        "07a827233cc275019deb9b43f6531d73cdd08f0e6eeb90b660d5511a1500b95b",
        "0f787b5593e5f6e22adcf0b5ad8871f968acbe504170c92caa8d92e00fccc28d",
    ),
    (
        "7729-102255-0005",
        "935720974fe96ff02339c814290c5b645981a1efe0acbf655946c72b2226c078",
        "313f09f26fb3a7ce82c66631c1688f05a581f439b21d4151dc8e6d7ee1dcc0b1",
    ),
    (
        "7729-102255-0007",
        "8ede2d4a434792f423affe0b509469950bb6e2f3b4d20356d2d384a00f579f0b",
        "d76bb4c7171b9dc6a595b1e3dcc368d4852c8e5e633b22f81bdee3016b75c0e4",
    ),
    (
        "7729-102255-0010",
        "1c2e7ef547202828bcf5d3d31e9ed13f33bd390f7604a58ab6275d9ea2169fbd",
        "a58cf955c033685ccf0a89fc4824f108188e21d4215558ce48e47b1b524c9b3a",
    ),
    (
        "2094-142345-0001",
        "b4fb8a76b5026b1cff5193cba298dc66156f93e228a221a06d26790c34cc049f",
        "4884cbe87d5103db44de11393ef44c9d9add3311427abbedf3d57d3161e6132a",
    ),
    (
        "2094-142345-0005",
        "d49d15ab4ba625ea6afaf5fcd9d7acc384d8817023f75c824083b9f4cb5c21a5",
        "087e83d2cfb8f9f3b6a24f4923e8911732caa41fdbfc29ea82c5bab7ae98406a",
    ),
    (
        "2094-142345-0018",
        "5ca1ee6260f5fd47dc20cc0e394f28848deedb018ae53319a40bdb7728275487",
        "27de0dc20eeb2274f21f2ae67056b20c3ff4fd9cd61a624f204d6bb580c3952d",
    ),
    (
        "3575-170457-0000",
        "96ab958dda879bdd4bc9fc61c2e5b0d632c569c256b54bcb1c0ecbeadd31448e",
        "a6ded73e69619b4576417d90f4d58973d7e51e1d5c17bb4ab5c1e1dc6079c7dc",
    ),
    (
        "3575-170457-0003",
        "a9e859bb34faf71044cb5099fa717b3dab58223c51ab0990db2002d0327b447e",
        "564428668e9a890c06f2478653814263c102edf87269059b33ac91285c84199d",
    ),
    (
        "3575-170457-0004",
        "53c16632587c7d8350765911a9892e9416b8cdee6ce11314aa4a1d29d3a9b935",
        "c02d4fe92a0414f931482a050c6d7a09a93c67d53cc30c3169be41569daa7420",
    ),
    (
        "2961-960-0001",
        "73a2c08a15dc492ae512877618f9bd2ba6dd8b3c1bd5a5e8d59ef63d7ef3d5fb",
        "24a163b87f7dc877af9d5a8d47cc7a2336bda8cda4b8769d89f131e97e7fb430",
    ),
    (
        "2961-960-0004",
        "c815b1d8d37cd643047430c39f790067c75ee3adb93fdfbbe3da493f71d44615",
        "34e185365d01f471c8838304ffba2984e85db9986b6130044c5403c340b298f1",
    ),
    (
        "2961-960-0005",
        "0d0c423dc55c236b5f55ca2fc4ab87b39b85d5d2a34585573afe6038ebf3a091",
        "5ca2db57ed169f606bd5386817f2e79a3d55f2adf949c8ccde0086c8d2574ba9",
    ),
    (
        "8463-287645-0000",
        "055fc9fb67be0272bde9aaf2019bb6a306c652a823ca62b0cb61cdf0a27c05c4",
        "fb6ae1d5d31ea260ae3cad5cae4de9106f48d5737627ec890f38bf2587d4a08c",
    ),
    (
        "8463-287645-0001",
        "a90eb7fbd2254cd455727a1be84279f9b909a46d8386fcdcad44918d3e809076",
        "4629f63779adc74a2eac398cb55919fe00000bf7e8be0be90ce15e22226cd37a",
    ),
    (
        "8463-287645-0004",
        "4bea5c3853d930cc16212d0ecdb9090e7b3d079927fa98b93983ad7d8e545263",
        "1b0aa86713d7771fcef37aacc44a2a9c0d2559a924b1c4656279b614223117dc",
    ),
)

NUMBER_WORDS = re.compile(
    r"\b(two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|\w+teen|twenty|"
    r"thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred|thousand|million|"
    r"third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|\w+teenth|\w+ieth)\b",
    re.IGNORECASE,
)
"""The selection rule's number words ("one", "first" and "second" are left
in: they are mostly not numbers)."""


@dataclass(frozen=True)
class Clip:
    id: str
    text: str  # the LibriSpeech transcript
    samples: NDArray[np.int16]  # 16 kHz mono


def fetch_clips(cache: Path) -> list[Clip]:
    """The clip set from ``cache``, downloading what is missing first."""
    cache.mkdir(parents=True, exist_ok=True)
    if not all(_cached(cache, *clip) for clip in CLIPS):
        with urllib.request.urlopen(LIBRISPEECH_URL, timeout=60) as response:
            print(f"downloading the clip set from {LIBRISPEECH_URL}", flush=True)
            extract_clips(response, cache)
    from voiced.api.ffmpeg import decode
    from voiced.audio import float32_to_pcm16

    clips = []
    for uid, _, _ in CLIPS:
        text = (cache / f"{uid}.txt").read_text()
        if NUMBER_WORDS.search(text):
            raise SystemExit(f"{uid}'s transcript has a number word: {text}")
        audio = asyncio.run(decode((cache / f"{uid}.flac").read_bytes()))
        clips.append(Clip(uid, text, np.frombuffer(float32_to_pcm16(audio), dtype="<i2")))
    return clips


def _cached(cache: Path, uid: str, flac_sha: str, text_sha: str) -> bool:
    flac, text = cache / f"{uid}.flac", cache / f"{uid}.txt"
    return (
        flac.is_file()
        and text.is_file()
        and _sha256(flac.read_bytes()) == flac_sha
        and _sha256(text.read_bytes()) == text_sha
    )


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def extract_clips(archive_stream: typing.BinaryIO, cache: Path) -> None:
    """Read the gzipped tar ``archive_stream`` once, keep the clips and their
    transcript lines, and stop once all are there. Bytes that differ from
    :data:`CLIPS` stop the run: the set must be the same on every Mac."""
    flac_sha = {uid: sha for uid, sha, _ in CLIPS}
    text_sha = {uid: sha for uid, _, sha in CLIPS}
    flacs: set[str] = set()
    texts: set[str] = set()
    with tarfile.open(fileobj=archive_stream, mode="r|gz") as archive:
        for member in archive:
            name = member.name.rsplit("/", 1)[-1]
            if name.endswith(".flac") and name[:-5] in flac_sha:
                uid = name[:-5]
                data = _read(archive, member)
                if _sha256(data) != flac_sha[uid]:
                    raise SystemExit(f"{uid}.flac does not match its sha256 in CLIPS")
                _write(cache / name, data)
                flacs.add(uid)
            elif name.endswith(".trans.txt"):
                for line in _read(archive, member).decode().splitlines():
                    uid, _, text = line.partition(" ")
                    if uid in text_sha:
                        if _sha256(text.encode()) != text_sha[uid]:
                            raise SystemExit(f"{uid}'s transcript does not match CLIPS")
                        _write(cache / f"{uid}.txt", text.encode())
                        texts.add(uid)
            if len(flacs) == len(texts) == len(CLIPS):
                return
    raise SystemExit(f"the archive lacks {sorted(set(flac_sha) - (flacs & texts))}")


def _read(archive: tarfile.TarFile, member: tarfile.TarInfo) -> bytes:
    stream = archive.extractfile(member)
    assert stream is not None
    return stream.read()


def _write(path: Path, data: bytes) -> None:
    partial = path.with_suffix(path.suffix + ".partial")
    partial.write_bytes(data)
    partial.replace(path)


# -- word error rate ---------------------------------------------------------

# Curly apostrophes count as straight ones; hyphens, dashes and slashes split words.
_SPELLING = str.maketrans({"æ": "ae", "œ": "oe", "ß": "ss", "\u2019": "'", "\u2018": "'"})
_SPACES = re.compile("[\\s\\-\u2010\u2011\u2013\u2014/]+")
_OTHER = re.compile(r"[^a-z0-9' ]")
_ABBREVIATIONS = {"mr": "mister", "mrs": "missus", "dr": "doctor"}
"""Spelled out as LibriSpeech's transcripts spell them."""


def normalize(text: str) -> list[str]:
    """The words of ``text`` as WER compares them: lower case, accents and
    punctuation removed, hyphens and dashes splitting words, apostrophes kept
    inside a word (``summer's``) and dropped around one (quotes), and
    ``Mr.``, ``Mrs.`` and ``Dr.`` spelled out."""
    text = unicodedata.normalize("NFKD", text.lower().translate(_SPELLING))
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = _OTHER.sub("", _SPACES.sub(" ", text))
    words = (w.strip("'") for w in text.split())
    return [_ABBREVIATIONS.get(word, word) for word in words if word]


def word_errors(reference: Sequence[str], hypothesis: Sequence[str]) -> int:
    """Substitutions, deletions and insertions turning ``reference`` into
    ``hypothesis``: the Levenshtein distance over words."""
    previous = list(range(len(hypothesis) + 1))
    for i, ref in enumerate(reference, 1):
        current = [i]
        for j, hyp in enumerate(hypothesis, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (ref != hyp)))
        previous = current
    return previous[-1]


def wer_percent(pairs: Iterable[tuple[str, str]]) -> float:
    """Corpus WER of ``(reference, hypothesis)`` pairs: every error over
    every reference word, so a long clip weighs more than a short one."""
    errors = words = 0
    for reference, hypothesis in pairs:
        ref = normalize(reference)
        errors += word_errors(ref, normalize(hypothesis))
        words += len(ref)
    if not words:
        raise ValueError("no reference words")
    return 100 * errors / words


# -- results -----------------------------------------------------------------


@dataclass(frozen=True)
class Latency:
    median_ms: float
    worst_ms: float
    n: int

    @classmethod
    def of(cls, values: list[float]) -> Latency:
        return cls(round(statistics.median(values), 1), round(max(values), 1), len(values))


@dataclass(frozen=True)
class STTMetrics:
    wer_percent: float
    clips: int
    batch_rtf: float  # transcription time over audio time, the clip set uploaded one by one
    # From the client's ``end`` to the final transcript, 64 and 100 ms chunks pooled.
    finalize_after_silence: Latency  # 200 ms of trailing silence
    finalize_at_last_word: Latency  # no trailing silence


@dataclass(frozen=True)
class TTSMetrics:
    voice: str
    utmos: float | None  # None only in a printed --no-naturalness run
    first_audio: Latency
    first_audio_while_stt_streams: Latency
    rtf: float  # synthesis time over audio time, a long reply


@dataclass(frozen=True)
class Result:
    kind: Kind
    engine: str
    model: str
    hardware: str  # model name, chip, memory: "MacBook Pro, Apple M1 Max, 64 GB"
    macos: str
    # The main commit the checkout branches from, with "+" when the measured
    # code (src/, scripts/, pyproject.toml, uv.lock) differs from it.
    commit: str
    version: str  # voiced's
    date: str
    languages: list[str]
    licence: str
    size_bytes: int | None  # the model's revision in the Hugging Face cache
    load_s: float | None  # None only in a printed --url run
    footprint_bytes: int | None
    mlx_load_peak_bytes: int | None  # None: the engine does not use MLX
    mlx_peak_bytes: int | None  # after use; the load peak included
    metrics: STTMetrics | TTSMetrics = field(repr=False)

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2) + "\n"

    @classmethod
    def from_json(cls, text: str) -> Result:
        data = json.loads(text)
        metrics = STTMetrics if data["kind"] == "stt" else TTSMetrics
        result: Result = decode(cls, {**data, "metrics": None})
        return dataclasses.replace(result, metrics=decode(metrics, data["metrics"]))

    @property
    def machine(self) -> str:
        return f"{self.hardware}, macOS {self.macos}"

    @property
    def complete(self) -> bool:
        """Whether every measurement was taken: only those are saved."""
        return self.load_s is not None and (
            not isinstance(self.metrics, TTSMetrics) or self.metrics.utmos is not None
        )

    def filename(self) -> str:
        parts = (self.kind, self.engine, self.model, self.hardware)
        return "-".join(_slug(p) for p in parts) + ".json"


def decode(cls: type[Any], data: dict[str, Any]) -> Any:
    """``cls`` from its JSON fields, nested dataclasses included. A field the
    data lacks takes its default; a field the data has that ``cls`` lacks
    (from a newer harness) is ignored."""
    hints = typing.get_type_hints(cls)
    values = {}
    for f in dataclasses.fields(cls):
        if f.name in data:
            values[f.name] = _decode_value(hints[f.name], data[f.name])
    return cls(**values)


def _decode_value(hint: Any, value: Any) -> Any:
    if value is None:
        return None
    for candidate in typing.get_args(hint) or (hint,):
        if dataclasses.is_dataclass(candidate) and isinstance(candidate, type):
            return decode(candidate, value)
    return value


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def load_results(directory: Path) -> list[Result]:
    return [Result.from_json(p.read_text()) for p in sorted(directory.glob("*.json"))]


def save(result: Result) -> Path:
    """Write ``result`` in place of an earlier run of the same model on the
    same hardware."""
    if not result.complete:
        raise SystemExit("a partial run is not saved")
    path = RESULTS_DIR / result.filename()
    if path.exists():
        earlier = Result.from_json(path.read_text())
        if (earlier.engine, earlier.model) != (result.engine, result.model):
            raise SystemExit(f"{path.name} holds {earlier.engine} {earlier.model}")
    RESULTS_DIR.mkdir(exist_ok=True)
    _write(path, result.to_json().encode())
    return path


# -- ratings -----------------------------------------------------------------


def rated(result: Result) -> list[tuple[Scale, float]]:
    """Each scale a result is rated on, with the number it rates."""
    return ratings.rated(result.kind, asdict(result.metrics))


def rate(result: Result) -> Rating:
    return ratings.rate(rated(result))


# -- the document ------------------------------------------------------------

DOC_HEADER = """\
# Benchmarks

<!-- Generated by scripts/bench_engine.py from docs/benchmarks/*.json. Do not edit. -->

Every engine and model voiced ships, measured the same way on named Macs, so
you can choose one by its numbers. The protocol, the clip set and why the
ratings are what they are: [ADR 0024](decisions/0024-engine-benchmarks.md).
Each row names its machine, the voiced version and commit measured and the
date; numbers from different machines do not compare.

To add or refresh a row, start nothing and run, from a checkout with
`uv sync --group dev --all-extras`, on a Mac with nothing else running:

```sh
uv run python scripts/bench_engine.py stt --engine <name> [--model <repo>]
uv run python scripts/bench_engine.py tts --engine <name> [--model <repo>]
```

It writes the result to `docs/benchmarks/` and regenerates this file;
`uv run python scripts/bench_engine.py render` only regenerates it.
"""


def _ms(latency: Latency) -> str:
    return f"{latency.median_ms:.0f} / {latency.worst_ms:.0f} ms"


def _gb(n: int | None) -> str:
    return "none" if n is None else f"{n / 1e9:.2f} GB"


def _languages(languages: list[str]) -> str:
    return ", ".join(languages) if len(languages) <= 3 else f"{len(languages)} languages"


def _size(n: int | None) -> str:
    from voiced.models import size_text

    return "not cached" if n is None else size_text(n)


def _stars(n: int | None) -> str:
    return "not rated" if n is None else f"{n} of 5"


def _common_cells(r: Result) -> list[str]:
    load = "not measured" if r.load_s is None else f"{r.load_s:.1f} s"
    return [
        load,
        "not reported" if r.footprint_bytes is None else _gb(r.footprint_bytes),
        f"{_gb(r.mlx_load_peak_bytes)} / {_gb(r.mlx_peak_bytes)}",
        _size(r.size_bytes),
        _languages(r.languages),
        r.licence,
        r.machine,
        f"{r.version} ({r.commit}), {r.date}",
    ]


COMMON_HEADERS = [
    "Load",
    "Footprint",
    "MLX peak, load / after use",
    "Size",
    "Languages",
    "Licence",
    "Machine",
    "Version (commit), date",
]
STT_HEADERS = [
    "Model",
    "Engine",
    "Quality",
    "Speed",
    "WER",
    "Finalize after 200 ms silence, median / worst",
    "Finalize at the last word, median / worst",
    "Batch RTF",
    *COMMON_HEADERS,
]
TTS_HEADERS = [
    "Model",
    "Engine",
    "Quality",
    "Speed",
    "UTMOS",
    "First audio, median / worst",
    "First audio while STT streams",
    "RTF",
    "Voice",
    *COMMON_HEADERS,
]


def row(r: Result) -> list[str]:
    rating = rate(r)
    head = [f"`{r.model}`", r.engine, _stars(rating.quality), _stars(rating.speed)]
    m = r.metrics
    if isinstance(m, STTMetrics):
        body = [
            f"{m.wer_percent:.1f}%",
            _ms(m.finalize_after_silence),
            _ms(m.finalize_at_last_word),
            f"{m.batch_rtf:.3f}",
        ]
    else:
        body = [
            "not scored" if m.utmos is None else f"{m.utmos:.2f}",
            _ms(m.first_audio),
            _ms(m.first_audio_while_stt_streams),
            f"{m.rtf:.3f}",
            f"`{m.voice}`",
        ]
    return [*head, *body, *_common_cells(r)]


def markdown_table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    lines += ["| " + " | ".join(cells) + " |" for cells in rows]
    return "\n".join(lines) + "\n"


def _limit(scale: Scale, bound: float) -> str:
    return f"{'>=' if scale.higher_is_better else '<='} {bound:g}{scale.unit}"


def _scale_rows() -> list[list[str]]:
    return [
        [f"{s.kind.upper()} {s.axis}", s.measure, *(_limit(s, b) for b in s.bounds), "otherwise"]
        for s in SCALES
    ]


def render(results: Sequence[Result]) -> str:
    """docs/benchmarks.md for ``results``, rows in engine, model and
    machine order."""
    ordered = sorted(results, key=lambda r: (r.engine, r.model, r.machine))
    parts = [DOC_HEADER]
    sections: tuple[tuple[Kind, str, list[str], str], ...] = (
        (
            "stt",
            "Speech to text",
            STT_HEADERS,
            "WER: word error rate over 24 clips of LibriSpeech test-clean (Panayotov et "
            "al. 2015, CC BY 4.0, openslr.org/12), after normalizing case and "
            "punctuation. Finalize: from the client's `end` to the final transcript, "
            "streaming in real time in 64 and 100 ms chunks. RTF: processing time over "
            "audio time.",
        ),
        (
            "tts",
            "Text to speech",
            TTS_HEADERS,
            "UTMOS: predicted naturalness, 1 to 5, the mean over three fixed "
            "sentences. First audio: from the request to the first audio byte for "
            '"Turn on the kitchen lights." Every engine serves 24 kHz mono 16-bit audio.',
        ),
    )
    for kind, title, headers, legend in sections:
        rows = [row(r) for r in ordered if r.kind == kind]
        parts.append(f"\n## {title}\n\n{legend}\n\n")
        parts.append(markdown_table(headers, rows) if rows else "No results yet.\n")
    parts.append(
        "\n## Ratings\n\nQuality and speed, 1 to 5, come from the numbers above by "
        "these limits (`voiced.ratings`). Speed is the lower of its two ratings (the "
        "latency and the throughput a user waits on). The app's model list will show "
        "them (ADR 0024 section 5).\n\n"
    )
    parts.append(markdown_table(["Rating", "Measure", "5", "4", "3", "2", "1"], _scale_rows()))
    return "".join(parts)


def write_doc() -> None:
    _write(DOC, render(load_results(RESULTS_DIR)).encode())


# -- the service -------------------------------------------------------------

READY_TIMEOUT_S = 900.0  # a first start loads the model from a cold disk
ALIGNMENTS = 32
CHUNKS = (1024, 1600)  # 64 and 100 ms at 16 kHz, as ADR 0008 measures
TRIALS = 10
STREAM_TO = "parakeet"  # the STT a tts run streams to: the default
INSTALLED_PORTS = (8880, 8881, 10200, 10300)


@dataclass(frozen=True)
class Server:
    url: str
    load_s: float | None


def _free_ports(n: int) -> list[int]:
    """``n`` distinct free ports, all held until all are chosen."""
    with contextlib.ExitStack() as stack:
        sockets = [stack.enter_context(socket.socket()) for _ in range(n)]
        for s in sockets:
            s.bind(("127.0.0.1", 0))
        return [int(s.getsockname()[1]) for s in sockets]


def bench_config(workdir: Path) -> Path:
    """A config of the bench's own that keeps the caller's Hugging Face home,
    so the service finds the models where this process sizes them."""
    from huggingface_hub import constants

    config = workdir / "config.toml"
    config.write_text(f"[install]\nhf_home = {json.dumps(str(constants.HF_HOME))}\n")
    return config


@contextmanager
def started(kind: Kind, engine: str, model: str, workdir: Path) -> Iterator[Server]:
    """``voiced serve <kind>`` on free ports with :func:`bench_config` (no
    ``VOICED_`` variable of the caller's shell applies), once it is ready.
    :func:`run` checks that it serves the engine and model named."""
    from huggingface_hub import constants

    from voiced.models import ModelNotDownloaded, not_downloaded

    # The services never download (ADR 0022): refuse now rather than wait
    # out READY_TIMEOUT_S on a service reporting missing_model.
    missing = not_downloaded(Path(constants.HF_HOME) / "hub", model)
    if missing:
        shared = () if model in missing else tuple(missing)
        raise SystemExit(str(ModelNotDownloaded(model, missing=shared)))
    workdir.mkdir(parents=True, exist_ok=True)
    http, wyoming = _free_ports(2)
    section = f"VOICED_{kind.upper()}__"
    env = {k: v for k, v in os.environ.items() if not k.startswith("VOICED_")}
    env |= {
        "VOICED_CONFIG": str(bench_config(workdir)),
        "VOICED_SERVER__LOG_DIR": str(workdir / "logs"),
        f"{section}ENGINE": engine,
        f"{section}MODEL": model,
        f"{section}HTTP_PORT": str(http),
        f"{section}WYOMING_PORT": str(wyoming),
    }
    voiced = Path(sys.executable).with_name("voiced")
    log = workdir / f"{kind}-{engine}.log"
    url = f"http://127.0.0.1:{http}"
    with log.open("ab") as out:
        spawned = time.perf_counter()
        proc = subprocess.Popen([str(voiced), "serve", kind], env=env, stdout=out, stderr=out)
    try:
        while not _ready(url):
            if proc.poll() is not None:
                tail = "\n".join(log.read_text(errors="replace").splitlines()[-20:])
                raise SystemExit(f"voiced serve {kind} exited with {proc.returncode}:\n{tail}")
            if time.perf_counter() - spawned > READY_TIMEOUT_S:
                raise SystemExit(f"voiced serve {kind} was not ready after {READY_TIMEOUT_S:.0f} s")
            time.sleep(0.05)
        yield Server(url, round(time.perf_counter() - spawned, 1))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def _ready(url: str) -> bool:
    try:
        return httpx.get(f"{url}/health", timeout=2).status_code == 200
    except httpx.TransportError:
        return False


@contextmanager
def service(kind: Kind, engine: str, model: str, workdir: Path) -> Iterator[Server]:
    """The service started twice; the second start is the one measured."""
    print(f"starting {kind} {engine} {model} (first start)", flush=True)
    with started(kind, engine, model, workdir):
        pass
    print(f"starting {kind} {engine} {model} (measured start)", flush=True)
    with started(kind, engine, model, workdir) as server:
        print(f"  ready in {server.load_s:.1f} s", flush=True)
        yield server


def warn_if_busy() -> None:
    """Say so when other voiced services are running: they share the GPU."""
    from voiced import gpu

    answering = []
    for port in INSTALLED_PORTS:
        with socket.socket() as s:
            s.settimeout(0.2)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                answering.append(port)
    if answering or gpu.stt_running():
        print(
            "warning: other voiced services are running (ports "
            f"{', '.join(map(str, answering)) or 'none'}, GPU lock "
            f"{'held' if gpu.stt_running() else 'free'}); they can slow the numbers",
            flush=True,
        )


# -- measurements ------------------------------------------------------------


def measure_stt(url: str, key: str, clips: list[Clip]) -> STTMetrics:
    client = bench.openai_client(url, key)

    def transcribe(clip: Clip) -> tuple[str, float]:
        wav = bench.wav_container(clip.samples, bench.STT_SAMPLE_RATE)
        started_at = time.perf_counter()
        text = client.audio.transcriptions.create(
            model="voiced", file=(f"{clip.id}.wav", wav, "audio/wav"), language="en"
        ).text
        return str(text), time.perf_counter() - started_at

    transcribe(clips[0])  # warm
    pairs: list[tuple[str, str]] = []
    busy = audio = 0.0
    for clip in clips:
        text, seconds = transcribe(clip)
        pairs.append((clip.text, text))
        busy += seconds
        audio += len(clip.samples) / bench.STT_SAMPLE_RATE
        errors = word_errors(normalize(clip.text), normalize(text))
        if errors:
            print(f"  {clip.id}: {errors} errors\n    ref: {clip.text}\n    hyp: {text}")
    if audio <= 0:
        raise ValueError("no audio")

    samples, rate = bench.load_wav_int16(bench.DEFAULT_WAV)
    ws_url = bench.stream_url(url)
    finalize = [
        Latency.of(
            [
                ms
                for chunk in CHUNKS
                for ms in bench.ws_finalize_ms(
                    ws_url, key, samples, rate, chunk, ALIGNMENTS, trailing
                )
            ]
        )
        for trailing in (bench.SILENCE_WINDOW_S, 0.0)
    ]
    return STTMetrics(
        wer_percent=round(wer_percent(pairs), 2),
        clips=len(clips),
        batch_rtf=round(busy / audio, 4),
        finalize_after_silence=finalize[0],
        finalize_at_last_word=finalize[1],
    )


NATURALNESS_TEXTS = (
    "The kitchen lights are on, and the thermostat is set to twenty degrees.",
    "Tomorrow will be cloudy in the morning, with sunny spells by the afternoon.",
    "I have added milk, eggs and a loaf of bread to your shopping list.",
)
SCORER_PINS = ("torch==2.14.1", "torchaudio==2.11.0")


def utmos(wavs: list[Path]) -> float | None:
    """The mean UTMOS of ``wavs``, from ``scripts/utmos_score.py`` in a
    throwaway environment; None, saying why, when it cannot run."""
    command = ["uv", "run", "--isolated", "--no-project", "--python", "3.12"]
    command += [f"--with={pin}" for pin in SCORER_PINS]
    command += ["python", str(SCORER), *map(str, wavs)]
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=1800, check=True)
        scores = [float(s) for s in json.loads(done.stdout.strip().splitlines()[-1])]
        if len(scores) != len(wavs) or not all(map(math.isfinite, scores)):
            raise ValueError(f"unexpected scores {scores} for {len(wavs)} files")
    except (OSError, subprocess.SubprocessError, ValueError, IndexError, TypeError) as exc:
        stderr = getattr(exc, "stderr", None) or ""
        print(f"  naturalness not scored: {exc}\n{stderr[-2000:]}")
        return None
    print(f"  UTMOS per sentence: {scores}")
    return round(statistics.mean(scores), 2)


def pick_voice(url: str, key: str, wanted: str | None) -> str:
    """``wanted``, else the configured default voice if the engine serves it,
    else the engine's first voice."""
    if wanted:
        return wanted
    from voiced.config import TTSConfig

    listed = httpx.get(f"{url}/v1/audio/voices", headers=bench.auth_headers(key), timeout=10)
    voices = [v["id"] for v in listed.json()["data"]]
    default = TTSConfig().default_voice
    return default if default in voices else voices[0]


def measure_tts(
    url: str, stt_url: str, key: str, voice: str, workdir: Path, naturalness: bool
) -> tuple[TTSMetrics, list[Path]]:
    """The measurements, and the naturalness sentences as WAV files for
    :func:`utmos` to score once the service has stopped."""
    first = bench.tts_first_audio_ms(url, key, voice, TRIALS)
    samples, rate = bench.load_wav_int16(bench.DEFAULT_WAV)
    speaking = bench.first_audio_while_streaming_ms(
        url, bench.stream_url(stt_url), key, voice, samples, rate, 3 * TRIALS
    )
    rtfs = []
    for _ in range(3):
        pcm, seconds = bench.synthesize(url, key, voice, bench.LONG_REPLY)
        if not pcm:
            raise SystemExit("the long reply came back empty")
        rtfs.append(seconds / (len(pcm) / 2 / bench.TTS_SAMPLE_RATE))
    wavs = []
    for i, text in enumerate(NATURALNESS_TEXTS if naturalness else ()):
        pcm, _ = bench.synthesize(url, key, voice, text)
        wav = workdir / f"naturalness-{i}.wav"
        wav.write_bytes(bench.wav_container(np.frombuffer(pcm, dtype="<i2"), bench.TTS_SAMPLE_RATE))
        wavs.append(wav)
    metrics = TTSMetrics(
        voice=voice,
        utmos=None,
        first_audio=Latency.of(first),
        first_audio_while_stt_streams=Latency.of(speaking),
        rtf=round(statistics.median(rtfs), 4),
    )
    return metrics, wavs


# -- the run -----------------------------------------------------------------


def machine() -> tuple[str, str]:
    """``("MacBook Pro, Apple M1 Max, 64 GB", "15.7.4")``: no host name."""
    out = subprocess.run(
        ["system_profiler", "-json", "SPHardwareDataType"], capture_output=True, check=True
    ).stdout
    hw = json.loads(out)["SPHardwareDataType"][0]
    version = subprocess.run(
        ["sw_vers", "-productVersion"], capture_output=True, text=True, check=True
    ).stdout.strip()
    return f"{hw['machine_name']}, {hw['chip_type']}, {hw['physical_memory']}", version


MEASURED_PATHS = ("src", "scripts", "pyproject.toml", "uv.lock")


def commit() -> str:
    """The commit of ``origin/main`` this checkout branches from, with ``+``
    when the measured code differs from it (committed on a branch, changed
    or new); "unknown" outside a git checkout."""

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, check=True
        ).stdout.strip()

    try:
        try:
            base = git("merge-base", "HEAD", "origin/main")
        except subprocess.CalledProcessError:
            # No origin/main: whether HEAD is on main cannot be told.
            return git("rev-parse", "--short=7", "HEAD") + "+"
        changed = git("diff", "--name-only", base, "--", *MEASURED_PATHS)
        new = git("ls-files", "--others", "--exclude-standard", "--", *MEASURED_PATHS)
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return base[:7] + ("+" if changed or new else "")


def size_on_disk(model: str) -> int | None:
    """Bytes of a locked ``model``'s files, its own also when it is one folder
    of a repository other models share; None unless the cache holds them
    all. For a model outside the lock: of the revision ``main`` points at in
    the Hugging Face cache, else of the largest one cached."""
    from huggingface_hub import constants

    from voiced.models import cached_revisions, locked, missing_files

    # The hub under the HF_HOME bench_config() gives the service.
    hub = Path(constants.HF_HOME) / "hub"
    entry = locked(model)
    if entry is not None:
        return None if missing_files(hub, entry) else entry.bytes
    revisions = cached_revisions(hub, {model}).revisions
    if not revisions:
        return None
    chosen = next((r for r in revisions if "main" in r.refs), None)
    return (chosen or max(revisions, key=lambda r: r.bytes)).bytes


def engine_class(kind: Kind, name: str) -> Any:
    from voiced.engines.registry import get_stt_engine, get_tts_engine

    return get_stt_engine(name) if kind == "stt" else get_tts_engine(name)


def model_metadata(kind: Kind, name: str, model: str) -> tuple[list[str], str]:
    """The languages and licence of ``model``, from the engine built, unloaded,
    for it: a model's own, not the engine's default model's."""
    from voiced.engines.registry import create_engine

    engine = create_engine(kind, name, model)
    return list(engine.languages), str(engine.license)


def mlx_peak(url: str) -> int | None:
    """MLX's peak allocation since the service started; None when the
    process never allocated through MLX (Phonon, on the CPU, reports 0)."""
    mlx = bench.memory(url).get("mlx")
    return (mlx["peak_bytes"] or None) if mlx else None


def served_model(url: str, key: str) -> str:
    listed = httpx.get(f"{url}/v1/models", headers=bench.auth_headers(key), timeout=10)
    model: str = listed.json()["data"][0]["id"]
    return model


def run(args: argparse.Namespace) -> Result:
    kind: Kind = args.kind
    cls = engine_class(kind, args.engine)
    model: str = args.model or cls.model_id
    key: str = args.api_key
    hardware, macos = machine()
    measured_commit = commit()
    clips = fetch_clips(args.cache) if kind == "stt" else []
    naturalness = kind == "tts" and not args.no_naturalness
    if not args.url:
        warn_if_busy()
    wavs: list[Path] = []
    with tempfile.TemporaryDirectory(prefix="voiced-bench-") as tmp:
        with _measured(kind, args, model, Path(tmp)) as (server, stt_url):
            served = bench.engine_name(server.url)
            if served != args.engine or served_model(server.url, key) != model:
                raise SystemExit(f"{server.url} does not serve {args.engine} {model}")
            load_peak = mlx_peak(server.url)
            metrics: STTMetrics | TTSMetrics
            if kind == "stt":
                metrics = measure_stt(server.url, key, clips)
            else:
                voice = pick_voice(server.url, key, args.voice)
                tts_metrics, wavs = measure_tts(
                    server.url, stt_url, key, voice, Path(tmp), naturalness
                )
                metrics = tts_metrics
            time.sleep(bench.SETTLE_S)
            footprint = bench.memory(server.url).get("footprint_bytes")
            peak = mlx_peak(server.url)
        if wavs:
            score = utmos(wavs)
            if score is None:
                raise SystemExit("naturalness was not scored; nothing saved")
            metrics = dataclasses.replace(tts_metrics, utmos=score)
    from voiced import __version__

    languages, licence = model_metadata(kind, args.engine, model)
    return Result(
        kind=kind,
        engine=args.engine,
        model=model,
        hardware=hardware,
        macos=macos,
        commit=measured_commit,
        version=__version__,
        date=date.today().isoformat(),
        languages=languages,
        licence=licence,
        size_bytes=size_on_disk(model),
        load_s=server.load_s,
        footprint_bytes=footprint,
        mlx_load_peak_bytes=load_peak,
        mlx_peak_bytes=peak,
        metrics=metrics,
    )


@contextmanager
def _measured(
    kind: Kind, args: argparse.Namespace, model: str, workdir: Path
) -> Iterator[tuple[Server, str]]:
    """The service to measure, and for tts the STT service to stream to:
    the ones given by URL, else started here."""
    given_stt: str = args.stt_url if kind == "tts" and args.stt_url else ""
    with _stt_to_stream_to(kind, given_stt, workdir) as stt:
        if args.url:
            yield Server(args.url.rstrip("/"), None), stt
        else:
            with service(kind, args.engine, model, workdir) as server:
                yield server, stt


@contextmanager
def _stt_to_stream_to(kind: Kind, given: str, workdir: Path) -> Iterator[str]:
    if kind == "stt" or given:
        yield given
        return
    print(f"starting stt {STREAM_TO} to stream to", flush=True)
    with started("stt", STREAM_TO, engine_class("stt", STREAM_TO).model_id, workdir) as stt:
        yield stt.url


def report(result: Result) -> None:
    headers = STT_HEADERS if result.kind == "stt" else TTS_HEADERS
    print("\n" + markdown_table(headers, [row(result)]))
    rating = rate(result)
    print(f"quality {_stars(rating.quality)}, speed {_stars(rating.speed)}, from:")
    for scale, value in rated(result):
        print(f"  {scale.axis}: {scale.measure} {value:g}{scale.unit} -> {scale.rate(value)}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Benchmark one engine and regenerate docs/benchmarks.md (ADR 0024)."
    )
    sub = parser.add_subparsers(dest="kind", required=True)
    for kind in ("stt", "tts"):
        p = sub.add_parser(kind, help=f"benchmark a {kind} engine")
        p.add_argument("--engine", required=True, help="the engine's registry name")
        p.add_argument("--model", help="model repository (default: the engine's model_id)")
        p.add_argument(
            "--url", help="measure this running service instead of starting one; prints only"
        )
        p.add_argument(
            "--api-key",
            default=os.environ.get("VOICED_API_KEY", ""),
            help="bearer key for a --url service that sets server.api_key "
            "(default: $VOICED_API_KEY)",
        )
        p.add_argument("--no-save", action="store_true", help="print the row; write nothing")
        if kind == "stt":
            p.add_argument(
                "--cache",
                type=Path,
                default=DEFAULT_CACHE,
                help=f"where the clip set is kept (default: {DEFAULT_CACHE})",
            )
        else:
            p.add_argument("--voice", help="voice to measure (default: the service default)")
            p.add_argument("--stt-url", help="STT service to stream to (default: start one)")
            p.add_argument(
                "--no-naturalness", action="store_true", help="skip the UTMOS score; prints only"
            )
    sub.add_parser("render", help="regenerate docs/benchmarks.md from docs/benchmarks/")
    args = parser.parse_args(argv)
    if args.kind == "render":
        write_doc()
        return 0
    result = run(args)
    report(result)
    if args.no_save or not result.complete:
        print("not saved: a partial run (--url, --no-naturalness) or --no-save")
        return 0
    path = save(result)
    write_doc()
    print(f"wrote docs/benchmarks/{path.name} and docs/benchmarks.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
