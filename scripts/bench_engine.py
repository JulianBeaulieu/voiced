#!/usr/bin/env python3
"""Benchmark one engine and keep docs/benchmarks.md in step with it (ADR 0024).

One command per engine kind; it starts the service itself, measures it, and
rates it::

    uv run python scripts/bench_engine.py stt --engine parakeet
    uv run python scripts/bench_engine.py tts --engine kokoro [--model REPO]

Each run prints its Markdown row and ratings, writes its result to
``docs/benchmarks/<kind>-<engine>-<model>-<machine>.json`` (replacing an
earlier run of the same model on the same hardware) and regenerates
``docs/benchmarks.md`` from every result there. ``render`` only
regenerates; a test fails when the document and the results differ.

``--url`` measures a service someone else started instead, as
``scripts/bench.py`` does; its load time is then not measured. Like
``bench.py``, this is a standalone script, so the ``openai`` client and the
measurement code stay off the daemon's path.

What a run measures (the protocol is ADR 0024's):

* the service started twice in a row, the second start timed from spawn to
  ``/health`` answering 200 (the first downloads a missing model and warms
  the disk cache);
* stt: word error rate and real-time factor of batch transcription over 24
  LibriSpeech test-clean utterances (:data:`CLIPS`, downloaded once into
  ``--cache``), and streaming finalize over ``bench.py``'s fixture at 32
  alignments of 64 ms chunks, after 200 ms of trailing silence and after
  none (ADR 0008's method);
* tts: first audio for ``bench.py``'s sentence, alone and while a second
  client streams to an STT service (the default engine, started alongside
  unless ``--stt-url``), the real-time factor of a long reply, and the
  UTMOS naturalness of :data:`NATURALNESS_TEXTS`, scored by
  ``scripts/utmos_score.py`` in a throwaway uv environment;
* memory from the service's ``/metrics.json`` 1 s after use: the physical
  footprint and MLX's peak since start; the model's size in the Hugging
  Face cache; languages and licence from the engine class.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import socket
import statistics
import subprocess
import sys
import tarfile
import tempfile
import time
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
from websockets.exceptions import WebSocketException

REPO_ROOT = Path(__file__).resolve().parent.parent
RESULTS_DIR = REPO_ROOT / "docs" / "benchmarks"
DOC = REPO_ROOT / "docs" / "benchmarks.md"
SCORER = Path(__file__).resolve().parent / "utmos_score.py"
DEFAULT_CACHE = Path.home() / ".cache" / "voiced" / "bench"

Kind = Literal["stt", "tts"]

# -- the clip set ------------------------------------------------------------

LIBRISPEECH_URL = "https://www.openslr.org/resources/12/test-clean.tar.gz"
"""LibriSpeech test-clean (CC BY 4.0). The download stops at the last clip
needed, about 80 MB into the 346 MB archive."""

CLIPS: tuple[tuple[str, str], ...] = (
    # Four male and four female speakers, the first eight in the archive's
    # order to balance the two; per speaker the first three utterances by id
    # of 3 to 12 s whose transcript has no number words (an engine writing
    # "36" for "thirty six" is formatting, not a recognition error).
    # 172.5 s, 443 words. The id and the sha256 of its .flac.
    ("6930-75918-0000", "9ce35224156f071ab58eb7feb8a5ceae600f6f9f353da2a6cbf797b6b1ac8a23"),
    ("6930-75918-0002", "74ecfd628f49dca71995972c075fcc9904df804d49a4eb5b11fb712b3584f789"),
    ("6930-75918-0004", "e1c4755c2febfc6b77fde418821c57e2e980d84d4c79a73ff60c6e6dfc794eac"),
    ("1320-122612-0001", "0a79505430aa3995a69d287ed16f8368a889b652ff6f3f4f25c371e07f7f0a20"),
    ("1320-122612-0002", "bcf8ec844960246ee63b0c22e95beef40dfc0b6e5efae89ac7a0d78e5afe0c9b"),
    ("1320-122612-0003", "0c77177884f8558fe30c0c61ab80c1edd164f9a914fdeb0a60ccb7eaecb4b933"),
    ("260-123286-0001", "482a025752a99bedf0d539c055c80c1f0396f3f0b0094fd1d3ff89b3d26beb9e"),
    ("260-123286-0002", "25d5b388fb4d8f14752fbbfb9f1b020b4de121b9be7d20ee6632b060ab88bd82"),
    ("260-123286-0003", "07a827233cc275019deb9b43f6531d73cdd08f0e6eeb90b660d5511a1500b95b"),
    ("7729-102255-0005", "935720974fe96ff02339c814290c5b645981a1efe0acbf655946c72b2226c078"),
    ("7729-102255-0007", "8ede2d4a434792f423affe0b509469950bb6e2f3b4d20356d2d384a00f579f0b"),
    ("7729-102255-0010", "1c2e7ef547202828bcf5d3d31e9ed13f33bd390f7604a58ab6275d9ea2169fbd"),
    ("2094-142345-0001", "b4fb8a76b5026b1cff5193cba298dc66156f93e228a221a06d26790c34cc049f"),
    ("2094-142345-0005", "d49d15ab4ba625ea6afaf5fcd9d7acc384d8817023f75c824083b9f4cb5c21a5"),
    ("2094-142345-0018", "5ca1ee6260f5fd47dc20cc0e394f28848deedb018ae53319a40bdb7728275487"),
    ("3575-170457-0000", "96ab958dda879bdd4bc9fc61c2e5b0d632c569c256b54bcb1c0ecbeadd31448e"),
    ("3575-170457-0003", "a9e859bb34faf71044cb5099fa717b3dab58223c51ab0990db2002d0327b447e"),
    ("3575-170457-0004", "53c16632587c7d8350765911a9892e9416b8cdee6ce11314aa4a1d29d3a9b935"),
    ("2961-960-0001", "73a2c08a15dc492ae512877618f9bd2ba6dd8b3c1bd5a5e8d59ef63d7ef3d5fb"),
    ("2961-960-0004", "c815b1d8d37cd643047430c39f790067c75ee3adb93fdfbbe3da493f71d44615"),
    ("2961-960-0005", "0d0c423dc55c236b5f55ca2fc4ab87b39b85d5d2a34585573afe6038ebf3a091"),
    ("8463-287645-0000", "055fc9fb67be0272bde9aaf2019bb6a306c652a823ca62b0cb61cdf0a27c05c4"),
    ("8463-287645-0001", "a90eb7fbd2254cd455727a1be84279f9b909a46d8386fcdcad44918d3e809076"),
    ("8463-287645-0004", "4bea5c3853d930cc16212d0ecdb9090e7b3d079927fa98b93983ad7d8e545263"),
)


@dataclass(frozen=True)
class Clip:
    id: str
    text: str  # the LibriSpeech transcript
    samples: NDArray[np.int16]  # 16 kHz mono


def fetch_clips(cache: Path) -> list[Clip]:
    """The clip set from ``cache``, downloading what is missing first."""
    cache.mkdir(parents=True, exist_ok=True)
    if any(not _cached(cache, uid, sha) for uid, sha in CLIPS):
        _download_clips(cache)
    from voiced.api.ffmpeg import decode

    clips = []
    for uid, _ in CLIPS:
        audio = asyncio.run(decode((cache / f"{uid}.flac").read_bytes()))
        pcm = np.frombuffer(_pcm16(audio), dtype="<i2")
        clips.append(Clip(uid, (cache / f"{uid}.txt").read_text().strip(), pcm))
    return clips


def _pcm16(audio: NDArray[np.float32]) -> bytes:
    from voiced.audio import float32_to_pcm16

    return float32_to_pcm16(audio)


def _cached(cache: Path, uid: str, sha: str) -> bool:
    flac, text = cache / f"{uid}.flac", cache / f"{uid}.txt"
    return flac.is_file() and text.is_file() and _sha256(flac.read_bytes()) == sha


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _download_clips(cache: Path) -> None:
    """Stream the archive, keep the clips and their transcripts, and stop
    once all are there. A clip whose bytes differ from :data:`CLIPS` stops
    the run: the set must be the same on every Mac."""
    wanted = dict(CLIPS)
    flacs: set[str] = set()
    texts: set[str] = set()
    print(f"downloading the clip set from {LIBRISPEECH_URL}", flush=True)
    with urllib.request.urlopen(LIBRISPEECH_URL, timeout=60) as response:
        with tarfile.open(fileobj=response, mode="r|gz") as archive:
            for member in archive:
                name = member.name.rsplit("/", 1)[-1]
                if name.endswith(".flac") and name[:-5] in wanted:
                    uid = name[:-5]
                    data = _read(archive, member)
                    if _sha256(data) != wanted[uid]:
                        raise SystemExit(f"{uid}.flac does not match its sha256 in CLIPS")
                    _write(cache / name, data)
                    flacs.add(uid)
                elif name.endswith(".trans.txt"):
                    for line in _read(archive, member).decode().splitlines():
                        uid, _, text = line.partition(" ")
                        if uid in wanted:
                            _write(cache / f"{uid}.txt", text.encode())
                            texts.add(uid)
                if len(flacs) == len(texts) == len(wanted):
                    return
    raise SystemExit(f"the archive lacks {sorted(set(wanted) - (flacs & texts))}")


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


def normalize(text: str) -> list[str]:
    """The words of ``text`` as WER compares them: lower case, accents and
    punctuation removed, hyphens and dashes splitting words, apostrophes kept
    inside a word (``summer's``) and dropped around one (quotes)."""
    text = unicodedata.normalize("NFKD", text.lower().translate(_SPELLING))
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = _OTHER.sub("", _SPACES.sub(" ", text))
    return [word for word in (w.strip("'") for w in text.split()) if word]


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
    finalize_after_silence: Latency | None  # 200 ms of trailing silence; None: no streaming
    finalize_at_last_word: Latency | None  # no trailing silence


@dataclass(frozen=True)
class TTSMetrics:
    voice: str
    utmos: float | None  # None: the scorer could not run
    first_audio: Latency
    first_audio_while_stt_streams: Latency | None
    rtf: float  # synthesis time over audio time, a long reply


@dataclass(frozen=True)
class Result:
    kind: Kind
    engine: str
    model: str
    machine: str  # model name, chip, memory, macOS version
    commit: str  # "-dirty" when src/, pyproject.toml or uv.lock had changes
    date: str
    languages: list[str]
    licence: str
    size_bytes: int | None  # the model's revision in the Hugging Face cache
    load_s: float | None  # None: measured with --url
    footprint_bytes: int | None
    mlx_peak_bytes: int | None  # None: the engine does not use MLX
    metrics: STTMetrics | TTSMetrics = field(repr=False)

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2) + "\n"

    @classmethod
    def from_json(cls, text: str) -> Result:
        data = json.loads(text)
        raw = data.pop("metrics")
        metrics: STTMetrics | TTSMetrics
        if data["kind"] == "stt":
            for key in ("finalize_after_silence", "finalize_at_last_word"):
                raw[key] = Latency(**raw[key]) if raw[key] is not None else None
            metrics = STTMetrics(**raw)
        else:
            raw["first_audio"] = Latency(**raw["first_audio"])
            if raw["first_audio_while_stt_streams"] is not None:
                raw["first_audio_while_stt_streams"] = Latency(
                    **raw["first_audio_while_stt_streams"]
                )
            metrics = TTSMetrics(**raw)
        return cls(**data, metrics=metrics)

    def hardware(self) -> str:
        """The machine without its macOS version: a rerun after an update
        replaces the earlier row."""
        return self.machine.rsplit(", macOS", 1)[0]

    def filename(self) -> str:
        return f"{self.kind}-{self.engine}-{_slug(self.model)}-{_slug(self.hardware())}.json"


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def load_results(directory: Path) -> list[Result]:
    return [Result.from_json(p.read_text()) for p in sorted(directory.glob("*.json"))]


# -- ratings -----------------------------------------------------------------


@dataclass(frozen=True)
class Scale:
    """One measured number to a rating of 1 to 5. ``bounds`` are the limits
    for 5, 4, 3 and 2, best first; a value past the last is 1. A value on a
    bound gets the better rating."""

    measure: str
    unit: str
    bounds: tuple[float, float, float, float]
    higher_is_better: bool = False

    def rate(self, value: float) -> int:
        for rating, bound in zip((5, 4, 3, 2), self.bounds, strict=True):
            if (value >= bound) if self.higher_is_better else (value <= bound):
                return rating
        return 1


STT_WER = Scale("word error rate on the clip set", "%", (3, 5, 8, 12))
STT_FINALIZE = Scale("finalize after 200 ms of silence, worst", " ms", (50, 150, 300, 600))
STT_RTF = Scale("batch real-time factor", "", (0.05, 0.1, 0.2, 0.5))
TTS_UTMOS = Scale("UTMOS naturalness", "", (4.3, 4.0, 3.6, 3.0), higher_is_better=True)
TTS_FIRST_AUDIO = Scale("first audio, median", " ms", (100, 200, 400, 800))
TTS_RTF = Scale("real-time factor", "", (0.1, 0.25, 0.5, 1.0))


@dataclass(frozen=True)
class Rating:
    quality: int | None  # None: not measured
    speed: int


def rate(metrics: STTMetrics | TTSMetrics) -> Rating:
    """Quality from accuracy or naturalness; speed is the lower of the
    latency and the throughput rating, the slower path a user waits on."""
    if isinstance(metrics, STTMetrics):
        speeds = [STT_RTF.rate(metrics.batch_rtf)]
        if metrics.finalize_after_silence is not None:
            speeds.append(STT_FINALIZE.rate(metrics.finalize_after_silence.worst_ms))
        return Rating(STT_WER.rate(metrics.wer_percent), min(speeds))
    quality = TTS_UTMOS.rate(metrics.utmos) if metrics.utmos is not None else None
    speed = min(TTS_FIRST_AUDIO.rate(metrics.first_audio.median_ms), TTS_RTF.rate(metrics.rtf))
    return Rating(quality, speed)


# -- the document ------------------------------------------------------------

DOC_HEADER = """\
# Benchmarks

<!-- Generated by scripts/bench_engine.py from docs/benchmarks/*.json. Do not edit. -->

Every engine and model voiced ships, measured the same way on named Macs, so
you can choose one by its numbers. The protocol, the clip set and why the
ratings are what they are: [ADR 0024](decisions/0024-engine-benchmarks.md).
Each row names its machine, the commit measured and the date; numbers from
different machines do not compare.

To add or refresh a row, start nothing and run, from a checkout with
`uv sync --group dev --all-extras`:

```sh
uv run python scripts/bench_engine.py stt --engine <name> [--model <repo>]
uv run python scripts/bench_engine.py tts --engine <name> [--model <repo>]
```

It writes the result to `docs/benchmarks/` and regenerates this file;
`uv run python scripts/bench_engine.py render` only regenerates it.
"""


def _ms(latency: Latency | None) -> str:
    return "n/a" if latency is None else f"{latency.median_ms:.0f} / {latency.worst_ms:.0f} ms"


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
        f"{_gb(r.footprint_bytes)} / {_gb(r.mlx_peak_bytes)}",
        _size(r.size_bytes),
        _languages(r.languages),
        r.licence,
        r.machine,
        f"{r.commit}, {r.date}",
    ]


COMMON_HEADERS = [
    "Load",
    "Footprint / MLX peak",
    "Size",
    "Languages",
    "Licence",
    "Machine",
    "Commit, date",
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
    rating = rate(r.metrics)
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
            "n/a" if m.utmos is None else f"{m.utmos:.2f}",
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


RATED: tuple[tuple[str, Scale], ...] = (
    ("STT quality", STT_WER),
    ("STT speed", STT_FINALIZE),
    ("STT speed", STT_RTF),
    ("TTS quality", TTS_UTMOS),
    ("TTS speed", TTS_FIRST_AUDIO),
    ("TTS speed", TTS_RTF),
)


def _scale_rows() -> list[list[str]]:
    rows = []
    for rating, s in RATED:
        sign = ">=" if s.higher_is_better else "<="
        rows.append([rating, s.measure, *(f"{sign} {b:g}{s.unit}" for b in s.bounds), "otherwise"])
    return rows


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
            "WER: word error rate over the 24 LibriSpeech clips, after normalizing "
            "case and punctuation. Finalize: from the client's `end` to the final "
            "transcript, streaming in real time. RTF: processing time over audio time.",
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
        "these limits. Speed is the lower of its two ratings (the latency and the "
        "throughput a user waits on); a speech-to-text engine without streaming is "
        "rated on throughput alone. The model list in the app shows them.\n\n"
    )
    parts.append(markdown_table(["Rating", "Measure", "5", "4", "3", "2", "1"], _scale_rows()))
    return "".join(parts)


def write_doc() -> None:
    DOC.write_text(render(load_results(RESULTS_DIR)))


# -- the service -------------------------------------------------------------

READY_TIMEOUT_S = 900.0  # a first start may download gigabytes
ALIGNMENTS = 32
TRIALS = 10
STT_FALLBACK = ("parakeet", None)  # the STT a tts run streams to: the default


@dataclass(frozen=True)
class Server:
    url: str
    load_s: float | None


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
        return port


@contextmanager
def started(kind: Kind, engine: str, model: str, workdir: Path) -> Iterator[Server]:
    """``voiced serve <kind>`` on free ports with a config of its own (no
    ``VOICED_`` variable of the caller's shell applies), until it is ready."""
    workdir.mkdir(parents=True, exist_ok=True)
    config = workdir / "config.toml"
    config.write_text("")
    port = _free_port()
    section = f"VOICED_{kind.upper()}__"
    env = {k: v for k, v in os.environ.items() if not k.startswith("VOICED_")}
    env |= {
        "VOICED_CONFIG": str(config),
        "VOICED_SERVER__LOG_DIR": str(workdir / "logs"),
        f"{section}ENGINE": engine,
        f"{section}MODEL": model,
        f"{section}HTTP_PORT": str(port),
        f"{section}WYOMING_PORT": str(_free_port()),
    }
    voiced = Path(sys.executable).with_name("voiced")
    log = workdir / f"{kind}-{engine}.log"
    url = f"http://127.0.0.1:{port}"
    with log.open("ab") as out:
        spawned = time.perf_counter()
        proc = subprocess.Popen([str(voiced), "serve", kind], env=env, stdout=out, stderr=out)
    try:
        while True:
            if proc.poll() is not None:
                tail = "\n".join(log.read_text(errors="replace").splitlines()[-20:])
                raise SystemExit(f"voiced serve {kind} exited with {proc.returncode}:\n{tail}")
            if time.perf_counter() - spawned > READY_TIMEOUT_S:
                raise SystemExit(f"voiced serve {kind} was not ready after {READY_TIMEOUT_S:.0f} s")
            try:
                if httpx.get(f"{url}/health", timeout=2).status_code == 200:
                    break
            except httpx.TransportError:
                pass
            time.sleep(0.05)
        yield Server(url, round(time.perf_counter() - spawned, 1))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


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


# -- measurements ------------------------------------------------------------


def measure_stt(url: str, key: str, clips: list[Clip]) -> STTMetrics:
    client = bench._openai(url, key)

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

    samples, rate = bench.load_wav_int16(bench.DEFAULT_WAV)
    ws_url = f"{bench._ws(url)}/v1/audio/transcriptions/stream"
    finalize: list[Latency | None] = []
    for trailing in (bench.SILENCE_WINDOW_S, 0.0):
        try:
            values = bench.ws_finalize_ms(ws_url, key, samples, rate, 1024, ALIGNMENTS, trailing)
            finalize.append(Latency.of(values))
        except (WebSocketException, TimeoutError) as exc:
            print(f"  streaming failed ({type(exc).__name__}: {exc}); rated on batch alone")
            finalize.append(None)
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


def synthesize(url: str, key: str, voice: str, text: str) -> tuple[bytes, float]:
    """The whole reply as 24 kHz PCM, and the seconds it took."""
    started_at = time.perf_counter()
    pcm = (
        bench._openai(url, key)
        .audio.speech.create(model="voiced", voice=voice, input=text, response_format="pcm")
        .read()
    )
    return pcm, time.perf_counter() - started_at


def utmos(wavs: list[Path]) -> float | None:
    """The mean UTMOS of ``wavs``, from ``scripts/utmos_score.py`` in a
    throwaway environment; None, saying why, when it cannot run."""
    command = ["uv", "run", "--isolated", "--no-project", "--python", "3.12"]
    command += [f"--with={pin}" for pin in SCORER_PINS]
    command += ["python", str(SCORER), *map(str, wavs)]
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=1800, check=True)
    except (OSError, subprocess.SubprocessError) as exc:
        stderr = getattr(exc, "stderr", None) or ""
        print(f"  naturalness not scored: {exc}\n{stderr[-2000:]}")
        return None
    scores: list[float] = json.loads(done.stdout.strip().splitlines()[-1])
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
) -> TTSMetrics:
    first = bench.tts_first_audio_ms(url, key, voice, TRIALS)
    samples, rate = bench.load_wav_int16(bench.DEFAULT_WAV)
    ws_url = f"{bench._ws(stt_url)}/v1/audio/transcriptions/stream"
    speaking = bench.first_audio_while_streaming_ms(
        url, ws_url, key, voice, samples, rate, 3 * TRIALS
    )
    rtfs = []
    for _ in range(3):
        pcm, seconds = synthesize(url, key, voice, bench.LONG_REPLY)
        rtfs.append(seconds / (len(pcm) / 2 / 24_000))
    score = None
    if naturalness:
        wavs = []
        for i, text in enumerate(NATURALNESS_TEXTS):
            pcm, _ = synthesize(url, key, voice, text)
            wav = workdir / f"naturalness-{i}.wav"
            wav.write_bytes(bench.wav_container(np.frombuffer(pcm, dtype="<i2"), 24_000))
            wavs.append(wav)
        score = utmos(wavs)
    return TTSMetrics(
        voice=voice,
        utmos=score,
        first_audio=Latency.of(first),
        first_audio_while_stt_streams=Latency.of(speaking),
        rtf=round(statistics.median(rtfs), 4),
    )


# -- the run -----------------------------------------------------------------


def machine() -> str:
    """``MacBook Pro, Apple M1 Max, 64 GB, macOS 15.7.4``: no host name."""
    out = subprocess.run(
        ["system_profiler", "-json", "SPHardwareDataType"], capture_output=True, check=True
    ).stdout
    hw = json.loads(out)["SPHardwareDataType"][0]
    version = subprocess.run(
        ["sw_vers", "-productVersion"], capture_output=True, text=True, check=True
    ).stdout.strip()
    return f"{hw['machine_name']}, {hw['chip_type']}, {hw['physical_memory']}, macOS {version}"


def commit() -> str:
    def git(*args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, check=True
        ).stdout.strip()

    sha = git("rev-parse", "--short=7", "HEAD")
    dirty = git("status", "--porcelain", "--", "src", "pyproject.toml", "uv.lock")
    return f"{sha}-dirty" if dirty else sha


def size_on_disk(model: str) -> int | None:
    """Bytes of ``model``'s pinned revision in the Hugging Face cache, else
    of the revision ``main`` points at, else of the largest one cached."""
    from voiced.models import cache_dir, cached_revisions, pinned_revision

    revisions = cached_revisions(Path(cache_dir()), {model}).revisions
    if not revisions:
        return None
    pinned = pinned_revision(model)
    chosen = next((r for r in revisions if r.revision == pinned), None)
    chosen = chosen or next((r for r in revisions if "main" in r.refs), None)
    return (chosen or max(revisions, key=lambda r: r.bytes)).bytes


def engine_class(kind: Kind, name: str) -> Any:
    from voiced.engines.registry import get_stt_engine, get_tts_engine

    return get_stt_engine(name) if kind == "stt" else get_tts_engine(name)


def memory_after_use(url: str) -> tuple[int | None, int | None]:
    time.sleep(bench.SETTLE_S)
    mem = bench.memory(url)
    mlx = mem.get("mlx")
    # A process that never allocates through MLX (Phonon, on the CPU) reports 0.
    return mem.get("footprint_bytes"), (mlx["peak_bytes"] or None) if mlx else None


def run(args: argparse.Namespace) -> Result:
    kind: Kind = args.kind
    cls = engine_class(kind, args.engine)
    model: str = args.model or cls.model_id
    key: str = args.api_key
    clips = fetch_clips(args.cache) if kind == "stt" else []
    with (
        tempfile.TemporaryDirectory(prefix="voiced-bench-") as tmp,
        _measured(kind, args, model, Path(tmp)) as (server, stt_url),
    ):
        served = bench.engine_name(server.url)
        if served != args.engine:
            raise SystemExit(f"{server.url} runs {served}, not {args.engine}")
        metrics: STTMetrics | TTSMetrics
        if kind == "stt":
            metrics = measure_stt(server.url, key, clips)
        else:
            voice = pick_voice(server.url, key, args.voice)
            metrics = measure_tts(
                server.url, stt_url, key, voice, Path(tmp), not args.no_naturalness
            )
            # The long reply has run: memory after use, as bench.py reads it.
        footprint, peak = memory_after_use(server.url)
    return Result(
        kind=kind,
        engine=args.engine,
        model=model,
        machine=machine(),
        commit=commit(),
        date=date.today().isoformat(),
        languages=list(cls.languages),
        licence=str(cls.license),
        size_bytes=size_on_disk(model),
        load_s=server.load_s,
        footprint_bytes=footprint,
        mlx_peak_bytes=peak,
        metrics=metrics,
    )


@contextmanager
def _measured(
    kind: Kind, args: argparse.Namespace, model: str, workdir: Path
) -> Iterator[tuple[Server, str]]:
    """The service to measure, and for tts the STT service to stream to:
    the ones given by URL, else started here."""
    stt_url: str = args.stt_url if kind == "tts" and args.stt_url else ""
    with _maybe_stt(kind, stt_url, workdir) as stt:
        if args.url:
            yield Server(args.url.rstrip("/"), None), stt
        else:
            with service(kind, args.engine, model, workdir) as server:
                yield server, stt


@contextmanager
def _maybe_stt(kind: Kind, given: str, workdir: Path) -> Iterator[str]:
    if kind == "stt" or given:
        yield given
        return
    engine = STT_FALLBACK[0]
    print(f"starting stt {engine} to stream to", flush=True)
    model = engine_class("stt", engine).model_id
    with started("stt", engine, model, workdir) as stt:
        yield stt.url


def report(result: Result) -> None:
    headers = STT_HEADERS if result.kind == "stt" else TTS_HEADERS
    print("\n" + markdown_table(headers, [row(result)]))
    rating = rate(result.metrics)
    m = result.metrics
    if isinstance(m, STTMetrics):
        print(f"quality {_stars(rating.quality)}: {STT_WER.measure} {m.wer_percent:.1f}%")
        detail = f"{STT_RTF.measure} {m.batch_rtf:.3f}"
        if m.finalize_after_silence is not None:
            detail += f", {STT_FINALIZE.measure} {m.finalize_after_silence.worst_ms:.0f} ms"
    else:
        print(f"quality {_stars(rating.quality)}: {TTS_UTMOS.measure} {m.utmos}")
        detail = (
            f"{TTS_FIRST_AUDIO.measure} {m.first_audio.median_ms:.0f} ms, "
            f"{TTS_RTF.measure} {m.rtf:.3f}"
        )
    print(f"speed {_stars(rating.speed)}: {detail}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Benchmark one engine and regenerate docs/benchmarks.md (ADR 0024)."
    )
    sub = parser.add_subparsers(dest="kind", required=True)
    for kind in ("stt", "tts"):
        p = sub.add_parser(kind, help=f"benchmark a {kind} engine")
        p.add_argument("--engine", required=True, help="the engine's registry name")
        p.add_argument("--model", help="model repository (default: the engine's model_id)")
        p.add_argument("--url", help="measure this running service instead of starting one")
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
            p.add_argument("--no-naturalness", action="store_true", help="skip the UTMOS score")
    sub.add_parser("render", help="regenerate docs/benchmarks.md from docs/benchmarks/")
    args = parser.parse_args(argv)
    if args.kind == "render":
        write_doc()
        return 0
    result = run(args)
    report(result)
    if not args.no_save:
        RESULTS_DIR.mkdir(exist_ok=True)
        (RESULTS_DIR / result.filename()).write_text(result.to_json())
        write_doc()
        print(f"wrote docs/benchmarks/{result.filename()} and docs/benchmarks.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
