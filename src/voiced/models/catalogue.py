"""What voiced can run: each engine, its models and the files it loads (ADR 0022).

The one file a new engine or model touches; then ``python -m voiced.models pin``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

# The catalogue -----------------------------------------------------------


@dataclass(frozen=True)
class Model:
    """A model: the files voiced loads from a Hugging Face repository.
    ``id`` defaults to the repository; models that share one repository
    (a folder each) need ids of their own.

    ``quality`` and ``speed`` are copied by hand from the model's reference
    row in docs/benchmarks/ (ADR 0024: its MacBook Pro, Apple M1 Max, 64 GB
    result), and a test checks them against that row; ``note`` says what
    the model is for."""

    repo: str
    license: str
    patterns: tuple[str, ...]
    gated: bool = False  # needs a token and acceptance of its terms
    id: str = ""
    quality: int | None = None  # 1 to 5, from the reference row; None: not measured
    speed: int | None = None  # 1 to 5, likewise
    note: str = ""  # one line by hand, at most 80 characters

    def __post_init__(self) -> None:
        if not self.id:
            object.__setattr__(self, "id", self.repo)

    @property
    def url(self) -> str:
        return f"https://huggingface.co/{self.repo}"


@dataclass(frozen=True)
class Engine:
    """``name`` is the entry point name ``engine =`` uses in the config;
    ``kind`` its registry group. ``shared`` models are downloaded with any
    of its models and deleted with the last."""

    name: str
    kind: Literal["stt", "tts"]
    models: tuple[Model, ...]
    shared: tuple[Model, ...] = ()


PHONON = ("config.json", "tokenizer.json", "model.q8.gguf", "default-voice.safetensors")
"""Phonon's config, tokenizer, q8 weights and default voice; not the other weight formats."""

PHONON_VOICES = (
    "NbpkqMVS3CJeq2j8",
    "NIWIOv5vTi5eRMj8",
    "vjFyp2BcCyTlUmYb",
    "4SZHfMpw-p46Ywgs",
    "bXwrkHhURL1VFJdG",
    "nGzMHMPAWGaJFHOo",
    "LMDIYE0Oa377Qh6Z",
    "40QrMVgAxFiUnP9f",
    "0zLApsZfVLLNaEGl",
    "_6Aslh2DxfmnRLmP",
    "qIxlZ7Ei8KvWlyQH",
    "d9XCyhjo4JhkHF1d",
    "6PWnV0Nq4wu7RVBT",
    "GgfEkEJtxZR7gnpy",
    "uem82D50GRv2Dwma",
    "y9bQqIDnyxwqK01t",
    "gDT1nz7Ie36ZhL-C",
    "m9MxpY6IUGXtWp76",
    "X6NceqF285Gr04gb",
    "YyKcxPG7iIgS3jsK",
    "zUCxzrOrugTSis03",
    "JOW0vchyswkQUMDS",
    "dME3IWyZBvmh1n1q",
    "Zn7g6NxLqXlJb7Qi",
    "gfP44RlHwr2kEwk7",
    "xPDpsV3F592srkjA",
    "nOykTmcH9ydsGGIc",
    "lX3LavMNdlIbgEHH",
    "dGyGDKo2osj7h3s7",
    "cXoIXJ2llS8S0WOW",
    "GXaSqMEmOg12OIYb",
    "xO8xQo3BrB8FuI6J",
    "cYtsRSxV5q9IMehn",
    "gSgG5Tor0GPEnqzC",
    "NIWoEurJHMJjyUTe",
    "y2eiKxtQo2B67deU",
    "QXNr0SvkukLB4wPq",
)
"""The English presets of ``gradium/phonon-voices`` that Phonon loads, by file
id (``voiced.engines.phonon`` names them; a test keeps the two in step)."""

POCKET_VOICE_FILES = ("alba", "marius", "javert", "fantine", "eponine", "azelma")
"""The voices of ``kyutai/pocket-tts-without-voice-cloning`` that Pocket TTS
loads, the six whose recordings allow commercial use (``voiced.engines.pocket``
names them; a test keeps the two in step)."""

CATALOGUE: tuple[Engine, ...] = (
    Engine(
        "parakeet",
        "stt",
        # What parakeet-mlx's from_pretrained reads.
        models=(
            Model(
                "mlx-community/parakeet-tdt-0.6b-v3",
                "CC-BY-4.0",
                ("config.json", "model.safetensors"),
                quality=5,
                speed=5,
                note="The default: accurate and fast, English and 24 European languages",
            ),
        ),
    ),
    Engine(
        "kokoro",
        "tts",
        # The config and the weights, with the voice packs in voices/; not
        # the sample WAVs or the .pt copies.
        models=(
            Model(
                "mlx-community/Kokoro-82M-bf16",
                "Apache-2.0",
                ("config.json", "*.safetensors"),
                quality=5,
                speed=5,
                note="The default: natural and fast, 28 American and British English voices",
            ),
        ),
    ),
    Engine(
        "soprano",
        "tts",
        # The configs, the tokenizer and the weights; not the README.
        models=(
            Model(
                "mlx-community/Soprano-80M-bf16",
                "Apache-2.0",
                ("*.json", "*.safetensors"),
                quality=4,
                speed=5,
                note="One expressive English voice; each reading of a text differs a little",
            ),
        ),
    ),
    Engine(
        "phonon",
        "tts",
        models=(
            Model(
                "gradium/phonon-31m-en",
                "Apache-2.0",
                PHONON,
                quality=5,
                speed=5,
                note="Runs on the CPU, leaving the GPU to speech to text; 38 English voices",
            ),
            # Non-commercial, gated. Of the 40M checkpoints, the English one.
            Model(
                "gradium/phonon-40m-en",
                "CC-BY-NC-4.0",
                PHONON,
                gated=True,
                note="The larger English model, for non-commercial use; not benchmarked yet",
            ),
        ),
        # The voice catalogue and its presets (ADR 0021).
        shared=(
            Model(
                "gradium/phonon-voices",
                "Apache-2.0",
                ("voices.jsonl", *(f"voices/{v}.safetensors" for v in PHONON_VOICES)),
            ),
        ),
    ),
    Engine(
        "pocket",
        "tts",
        # The config and the weights mlx-audio's Pocket TTS model reads.
        models=(
            Model("mlx-community/pocket-tts", "CC-BY-4.0", ("config.json", "model.safetensors")),
        ),
        # The tokenizer the config names and the voice embeddings (ADR 0028).
        shared=(
            Model(
                "kyutai/pocket-tts-without-voice-cloning",
                "CC-BY-4.0",
                ("tokenizer.model", *(f"embeddings/{v}.safetensors" for v in POCKET_VOICE_FILES)),
            ),
        ),
    ),
)

DEFAULT_ENGINES: dict[str, str] = {"stt": "parakeet", "tts": "kokoro"}
"""The engines a new config runs (``voiced.config``'s defaults)."""
