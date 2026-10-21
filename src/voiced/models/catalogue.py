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
    # BCP-47 tags it serves; empty: its engine's. Unlike the licence, which the
    # registry sets, an engine that serves per-model languages applies them itself.
    languages: tuple[str, ...] = ()

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


CHATTERBOX = (
    "config.json",
    "model.safetensors*",
    "conds.safetensors",
    "added_tokens.json",
    "merges.txt",
    "special_tokens_map.json",
    "tokenizer_config.json",
    "vocab.json",
)
"""Chatterbox Turbo's config, weights, text tokenizer and default voice
(``conds.safetensors``); not the model card."""

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

QWEN3_ASR = ("*.json", "*.safetensors", "merges.txt")
"""What mlx-audio loads from a Qwen3-ASR or Qwen3-ForcedAligner checkpoint."""

MOONSHINE_FILES = (
    "adapter.ort",
    "cross_kv.ort",
    "decoder_kv.ort",
    "encoder.ort",
    "frontend.model.ort",
    "frontend.weights.ort",
    "streaming_config.json",
    "tokenizer.bin",
)
"""What a Moonshine streaming model folder holds that the runtime loads
without word timestamps (ADR 0026)."""


MARVIS_TOKENIZER = (
    "tokenizer.json",
    "tokenizer_config.json",
    "special_tokens_map.json",
    "vocab.json",
    "merges.txt",
)
"""Marvis's text tokenizer, which its MLX conversions name by repository."""

MARVIS_VOICES = ("conversational_a", "conversational_b", "conversational_fr", "conversational_de")
"""The reference clips Marvis ships as voices, each a WAV and its transcript
(``voiced.engines.marvis`` names them; a test keeps the two in step)."""

MIMI = "tokenizer-e351c8d8-checkpoint125.safetensors"
"""Mimi, Kyutai's audio codec, which Marvis speaks in and mlx-audio fetches
from ``kyutai/moshiko-pytorch-bf16``."""


def _moonshine(size: str, quality: int, speed: int, note: str) -> Model:
    """A Moonshine English streaming model: one folder of the publisher's
    asset repository, named by its model card."""
    folder = f"model/{size}-streaming-en/quantized_26_08_21"
    return Model(
        "moonshine-ai/moonshine-voice-assets",
        "MIT",
        tuple(f"{folder}/{name}" for name in MOONSHINE_FILES),
        id=f"moonshine-ai/moonshine-streaming-{size}",
        quality=quality,
        speed=speed,
        note=note,
    )


NEMOTRON_LOCALES = (
    "en-US", "en-GB", "es-US", "es-ES", "fr-FR", "fr-CA", "it-IT", "pt-BR", "pt-PT",
    "nl-NL", "de-DE", "tr-TR", "ru-RU", "ar-AR", "hi-IN", "ja-JP", "ko-KR", "vi-VN",
    "uk-UA", "pl-PL", "sv-SE", "cs-CZ", "nb-NO", "da-DK", "bg-BG", "fi-FI", "hr-HR",
    "sk-SK", "zh-CN", "hu-HU", "ro-RO", "et-EE",
)  # fmt: skip
"""The 32 locales, in 28 languages, Nemotron 3.5 transcribes out of the box
(NVIDIA's card: "transcription-ready" and "broad-coverage"; the 8
"adaptation-ready" ones need fine-tuning first), English first. Each is a
key of the model's language prompts."""

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
            Model(
                "mlx-community/pocket-tts",
                "CC-BY-4.0",
                ("config.json", "model.safetensors"),
                quality=5,
                speed=4,
                note="Kyutai's model Phonon builds on, on the GPU; six CC0 and CC-BY voices",
            ),
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
    Engine(
        "chatterbox",
        "tts",
        # The conversions' cards say Apache-2.0, but Resemble AI released the
        # weights under MIT (ADR 0029). 4-bit is the default, provisionally.
        models=(
            Model(
                "mlx-community/chatterbox-turbo-4bit",
                "MIT",
                CHATTERBOX,
                quality=4,
                speed=3,
                note="One expressive English voice that performs tags like [laugh]",
            ),
            Model(
                "mlx-community/chatterbox-turbo-fp16",
                "MIT",
                CHATTERBOX,
                quality=4,
                speed=3,
                note="Chatterbox Turbo at full precision: three times the memory of 4-bit",
            ),
        ),
    ),
    Engine(
        "qwen3-asr",
        "stt",
        # The configs, the weights and the tokenizer's merges; not the README.
        models=(
            Model(
                "mlx-community/Qwen3-ASR-0.6B-8bit",
                "Apache-2.0",
                QWEN3_ASR,
                quality=5,
                speed=3,
                note="For files, with subtitles, and 30 languages; no live partials",
            ),
            Model(
                "mlx-community/Qwen3-ASR-1.7B-8bit",
                "Apache-2.0",
                QWEN3_ASR,
                quality=5,
                speed=2,
                note="The more accurate Qwen3-ASR, at about twice the time and memory",
            ),
        ),
        # The forced aligner, for segment times (ADR 0027).
        shared=(Model("mlx-community/Qwen3-ForcedAligner-0.6B-8bit", "Apache-2.0", QWEN3_ASR),),
    ),
    Engine(
        "moonshine",
        "stt",
        models=(
            _moonshine(
                "small",
                quality=4,
                speed=3,
                note="Runs on the CPU, leaving the GPU to text to speech; English, macOS 15+",
            ),
            _moonshine(
                "tiny",
                quality=3,
                speed=4,
                note="The fastest Moonshine and the least accurate; English, macOS 15+",
            ),
            _moonshine(
                "medium",
                quality=4,
                speed=3,
                note="The most accurate Moonshine, and the slowest to finalize; English, macOS 15+",
            ),
        ),
    ),
    Engine(
        "omnivoice",
        "tts",
        # The model, its text tokenizer and the audio codec with its licence;
        # not the README. Non-commercial weights (ADR 0030).
        models=(
            Model(
                "k2-fsa/OmniVoice",
                "CC-BY-NC; codec: Boson Higgs Audio 2 Community License",
                (
                    "config.json",
                    "model.safetensors",
                    "tokenizer.json",
                    "tokenizer_config.json",
                    "chat_template.jinja",
                    "audio_tokenizer/*",
                ),
                quality=5,
                speed=2,
                note="646 languages, Levantine Arabic among them; slow, for non-commercial use",
            ),
        ),
    ),
    Engine(
        "nemotron",
        "stt",
        # What mlx-audio reads: the MLX conversion keeps its vocabulary in
        # config.json; NVIDIA's English repository is in the Transformers
        # layout, beside a .nemo copy. The licences are in ADR 0025.
        models=(
            Model(
                "mlx-community/nemotron-3.5-asr-streaming-0.6b",
                "OpenMDW-1.1",
                ("config.json", "model.safetensors"),
                quality=5,
                speed=4,
                note="Streaming in 28 languages, detected when not given; slow uploads",
                languages=NEMOTRON_LOCALES,
            ),
            Model(
                "nvidia/nemotron-speech-streaming-en-0.6b",
                "NVIDIA Open Model License",
                ("config.json", "model.safetensors", "processor_config.json", "tokenizer.json"),
                quality=5,
                speed=4,
                note="Streaming, English only; a lower error rate in English than Nemotron 3.5",
                languages=("en-US",),
            ),
        ),
    ),
    Engine(
        "marvis",
        "tts",
        # The config and the weights mlx-audio's sesame model reads; Mimi, its
        # codec, is Kyutai's (ADR 0031).
        models=(
            Model(
                "Marvis-AI/marvis-tts-100m-v0.2-MLX-8bit",
                "Apache-2.0; codec: CC-BY-4.0",
                ("config.json", "model.safetensors*"),
                quality=4,
                speed=3,
                note="French and German voices, Apache-2.0; 3.04 GB with Parakeet, over the budget",
            ),
            # Not the default: no better here, more memory, and its French
            # voice runs on to the length bound more often (ADR 0031).
            Model(
                "Marvis-AI/marvis-tts-250m-v0.2-MLX-8bit",
                "Apache-2.0; codec: CC-BY-4.0",
                ("config.json", "model.safetensors*"),
                quality=3,
                speed=3,
                note="Larger, no better here; 3.23 GB beside Parakeet; one French line runs on",
            ),
        ),
        # The text tokenizer and the voices' reference clips, the same in the
        # 250M and 100M repositories but for the French and German clips only
        # the 250M has; and Mimi's weights.
        shared=(
            Model(
                "Marvis-AI/marvis-tts-250m-v0.2",
                "Apache-2.0",
                (
                    *MARVIS_TOKENIZER,
                    *(f"prompts/{v}.{e}" for v in MARVIS_VOICES for e in ("wav", "txt")),
                ),
            ),
            Model("kyutai/moshiko-pytorch-bf16", "CC-BY-4.0", (MIMI,)),
        ),
    ),
)

DEFAULT_ENGINES: dict[str, str] = {"stt": "parakeet", "tts": "kokoro"}
"""The engines a new config runs (``voiced.config``'s defaults)."""
