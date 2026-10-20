"""Phonon on the CPU through ``ptts``. Selectable, not the default.

Phonon is a small flow-matching transformer that predicts Mimi codec latents
80 ms at a time and decodes them to 24 kHz audio, run by the ``xn`` Rust
runtime in the ``ptts`` wheel (no PyTorch, no espeak-ng). This engine (see
``docs/decisions/0021-tts-phonon.md``):

* streams each sentence: the shared sentence loop yields Phonon's chunks as
  they are decoded, so first audio is the first chunk, not the sentence;
* runs on the CPU (``device="cpu"``, a pool of :data:`THREADS` threads), so
  it never waits for or holds the GPU the STT service goes first on
  (ADR 0019); each blocking step runs in a worker thread through
  ``run_blocking``, and ``ptts`` releases the GIL while it works;
* loads the model and the voices from voiced's pinned snapshots
  (``voiced.models``; the voices are the engine's ``shared`` repository in
  the catalogue), as local folders, so ``ptts`` never reaches the Hub
  itself and every voice is on disk before the first request.

Voices are Gradium's English presets from ``gradium/phonon-voices``, named
as the catalogue names them, plus ``default``, the checkpoint's own voice.
Text normalization is ``ptts``'s English one (``lang="en"``); voiced has
already normalized the text, so it mostly finds nothing left to do. Phonon
has no rate control: ``speed`` is checked against the shared bounds and
otherwise ignored.

Everything that imports ``ptts`` runs inside ``load()``, so this module
imports (the entry-point registry does so) without the ``phonon`` extra.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Generator
from typing import Any, Literal

from voiced.audio import float32_to_pcm16
from voiced.engines.base import TTS_SAMPLE_RATE, Voice
from voiced.engines.sentence import WARMUP_TEXT, SentenceTTSEngine
from voiced.models import CATALOGUE, default_model

log = logging.getLogger(__name__)

__all__ = ["DEFAULT_VOICE", "PHONON_VOICES", "PhononEngine"]

LANG = "en"
"""``ptts``'s text normalization; voiced is English-only (DECISIONS)."""
QUANT = "q8"
"""8-bit weights, ``model.q8.gguf``: what the model cards recommend on the CPU."""
THREADS = 4
"""``ptts``'s CPU thread pool. Its default is every core, efficiency cores
included, beside the STT's own CPU work; ADR 0021 has the measurement."""
_PHONON = next(e for e in CATALOGUE if e.name == "phonon")
VOICES_REPO = _PHONON.shared[0].repo
MODELS_40M = tuple(m.repo for m in _PHONON.models if "phonon-40m" in m.repo)
"""The 40M checkpoints in the catalogue; the engine refuses any other."""

DEFAULT_VOICE = Voice(id="default", name="Default", language="en", gender=None)

# The English rows of voices.jsonl at the pinned VOICES_REPO revision: name
# (the voice id), file id, locale, gender. Character voices have no locale.
_CATALOGUE: tuple[tuple[str, str, str, Literal["female", "male"] | None], ...] = (
    ("Zoey", "NbpkqMVS3CJeq2j8", "en-US", "female"),
    ("Juniper", "NIWIOv5vTi5eRMj8", "en-US", "female"),
    ("Everly", "vjFyp2BcCyTlUmYb", "en-US", "female"),
    ("Harper", "4SZHfMpw-p46Ywgs", "en-US", "female"),
    ("Hadley", "bXwrkHhURL1VFJdG", "en-US", "female"),
    ("Delia", "nGzMHMPAWGaJFHOo", "en-US", "female"),
    ("Easton", "LMDIYE0Oa377Qh6Z", "en-US", "male"),
    ("Micah", "40QrMVgAxFiUnP9f", "en-US", "male"),
    ("Trevor", "0zLApsZfVLLNaEGl", "en-US", "male"),
    ("Russell", "_6Aslh2DxfmnRLmP", "en-US", "male"),
    ("Preston", "qIxlZ7Ei8KvWlyQH", "en-US", "male"),
    ("Miles", "d9XCyhjo4JhkHF1d", "en-US", "male"),
    ("Maeve", "6PWnV0Nq4wu7RVBT", "en-GB", "female"),
    ("Freya", "GgfEkEJtxZR7gnpy", "en-GB", "female"),
    ("Pippa", "uem82D50GRv2Dwma", "en-GB", "female"),
    ("Elodie-Rose", "y9bQqIDnyxwqK01t", "en-GB", "female"),
    ("Imogen", "gDT1nz7Ie36ZhL-C", "en-GB", "female"),
    ("Lottie", "m9MxpY6IUGXtWp76", "en-GB", "female"),
    ("Alfie", "X6NceqF285Gr04gb", "en-GB", "male"),
    ("Hugh", "YyKcxPG7iIgS3jsK", "en-GB", "male"),
    ("Edwin", "zUCxzrOrugTSis03", "en-GB", "male"),
    ("Spencer", "JOW0vchyswkQUMDS", "en-GB", "male"),
    ("Toby", "dME3IWyZBvmh1n1q", "en-GB", "male"),
    ("Teddy", "Zn7g6NxLqXlJb7Qi", "en-GB", "male"),
    ("Bryony", "gfP44RlHwr2kEwk7", "en-GB", "female"),
    ("Captain Silas Blackwater", "xPDpsV3F592srkjA", "en", None),
    ("Morwenna of the Deep", "nOykTmcH9ydsGGIc", "en", None),
    ("Old Mortlake", "lX3LavMNdlIbgEHH", "en", None),
    ("Azgorath", "dGyGDKo2osj7h3s7", "en", None),
    ("Fennick the Young Elf", "cXoIXJ2llS8S0WOW", "en", None),
    ("The Thing Under the Stairs", "GXaSqMEmOg12OIYb", "en", None),
    ("Vyrmathrax the Ancient", "xO8xQo3BrB8FuI6J", "en", None),
    ("Grub the Ogre", "cYtsRSxV5q9IMehn", "en", None),
    ("Master Aldric", "gSgG5Tor0GPEnqzC", "en", None),
    ("The Snow Queen", "NIWoEurJHMJjyUTe", "en", None),
    ("Sir Bramwell", "y2eiKxtQo2B67deU", "en", None),
    ("Fairy Godmother", "QXNr0SvkukLB4wPq", "en", None),
)

VOICE_FILES: dict[str, str] = {
    name: f"voices/{file}.safetensors" for name, file, _, _ in _CATALOGUE
}
"""Each preset's file in :data:`VOICES_REPO`."""

PHONON_VOICES: list[Voice] = [
    DEFAULT_VOICE,  # first: an engine switch without a voice picks it
    *(Voice(id=name, name=name, language=tag, gender=g) for name, _, tag, g in _CATALOGUE),
]


class PhononEngine(SentenceTTSEngine):
    name = "phonon"
    model_id = default_model("phonon").repo
    languages = ["en-US", "en-GB"]
    license = default_model("phonon").license

    @classmethod
    def catalogue_voices(cls) -> list[Voice]:
        return list(PHONON_VOICES)

    def __init__(self, model_id: str | None = None) -> None:
        super().__init__(model_id)
        if "phonon-40m" in self.model_id and self.model_id not in MODELS_40M:
            raise ValueError(
                f"{self.model_id} is not in voiced's model catalogue; of the Phonon 40M "
                f"models it lists {', '.join(MODELS_40M)}"
            )

    def _load_sync(self) -> None:
        """Build in locals and assign ``_model`` last, so a failure part-way
        leaves the engine unloaded."""
        import ptts

        from voiced.models import snapshot

        started = time.perf_counter()
        root = snapshot(self.model_id, check_only=True)
        voices = snapshot(VOICES_REPO, check_only=True)
        ptts.set_num_threads(THREADS)  # process-wide, so before the model
        model = ptts.TTS(str(root), device="cpu", quant=QUANT, lang=LANG, voice_catalog=str(voices))
        rate = int(model.sample_rate)
        if rate != TTS_SAMPLE_RATE:
            raise RuntimeError(f"Phonon model reports {rate} Hz, expected {TTS_SAMPLE_RATE}")
        if DEFAULT_VOICE.id not in model.voices:
            raise RuntimeError(f"{self.model_id} has no default voice")
        for name, path in VOICE_FILES.items():
            model.add_voice(name, str(voices / path))
        b"".join(_stream(model, WARMUP_TEXT, DEFAULT_VOICE.id))
        self._model = model
        log.info(
            "phonon: loaded %s (%s, %s) with %d voices in %.1fs",
            self.model_id,
            model.quant,
            model.device,
            len(PHONON_VOICES),
            time.perf_counter() - started,
        )

    async def voices(self) -> list[Voice]:
        self._check_loaded()
        return list(PHONON_VOICES)

    def _has_voice(self, voice: str) -> bool:
        return voice == DEFAULT_VOICE.id or voice in VOICE_FILES

    def _synth_sentence(self, sentence: str, voice: str, speed: float) -> bytes:
        raise NotImplementedError("Phonon streams each sentence: see _synth_chunks")

    def _synth_chunks(
        self, sentence: str, voice: str, speed: float
    ) -> Generator[bytes, None, None]:
        """Blocking; each step runs in a worker thread. ``speed`` is ignored:
        the model has no rate control. A sentence without a letter or a digit
        is skipped: Phonon reads "..." or "!!!" as a second or more of sound."""
        if any(c.isalnum() for c in sentence):
            yield from _stream(self._model, sentence, voice)


def _stream(model: Any, sentence: str, voice: str) -> Generator[bytes, None, None]:
    """One sentence as 24 kHz int16 PCM pieces, as Phonon decodes them.
    Closing the generator stops Phonon's workers for the sentence."""
    with model.stream(sentence, voice=voice) as audio:
        for pcm in audio:
            yield float32_to_pcm16(pcm)
