"""Kyutai's Pocket TTS on the GPU through mlx-audio. Selectable, not the default.

Pocket TTS is the model Phonon builds on: a flow-matching transformer that
predicts Mimi codec latents one 80 ms frame at a time, and Mimi's decoder,
which turns them into 24 kHz audio. No phonemizer, no espeak-ng. This engine
(see ``docs/decisions/0028-tts-pocket.md``):

* streams each sentence frame by frame: the shared sentence loop yields every
  80 ms frame as it is decoded, so first audio is the first frame;
* runs on the GPU through MLX, so before each frame it waits for any
  speech-to-text encoder call that holds the GPU (``voiced.gpu``, ADR 0019),
  within the per-sentence budget;
* relies on the sentence loop running the load and every step on the
  engine's one model thread (``voiced.engines.sentence``): a sentence's
  generator keeps MLX arrays (the attention cache, Mimi's decoder state) from
  one frame to the next, and MLX cannot evaluate them on another thread;
* generates one sentence at a time: Mimi's streaming state lives on the
  model, so an overlapping second sentence is refused, not garbled;
* loads the model, the tokenizer and the voices from voiced's pinned
  snapshots (``voiced.models``, the catalogue), as local files, so mlx-audio
  never reaches the Hub itself.

The model is ``mlx-community/pocket-tts``, Kyutai's English checkpoint
converted for mlx-audio (CC-BY-4.0). The tokenizer and the voice embeddings
come from ``kyutai/pocket-tts-without-voice-cloning`` (CC-BY-4.0). The voices
are the six whose recordings allow commercial use (CC0 or CC-BY-4.0, per
``kyutai/tts-voices``); ``cosette`` (Expresso) and ``jean`` (EARS) are
CC-BY-NC and are not offered. Pocket TTS has no rate control: ``speed`` is
checked against the shared bounds and otherwise ignored.

Everything that imports MLX or mlx-audio runs inside ``load()``, so this
module imports (the entry-point registry does so) without the ``pocket``
extra.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Generator
from typing import Any, Literal, cast

import numpy as np

from voiced import gpu
from voiced.audio import float32_to_pcm16
from voiced.engines.base import TTS_SAMPLE_RATE, Voice
from voiced.engines.registry import ModelRefused
from voiced.engines.sentence import WARMUP_TEXT, SentenceTTSEngine
from voiced.models import default_model, engine

log = logging.getLogger(__name__)

__all__ = ["DEFAULT_VOICE", "POCKET_VOICES", "VOICES_REPO", "PocketEngine"]

_POCKET = engine("pocket")
if _POCKET is None:  # pragma: no cover - the catalogue ships with the engine
    raise ImportError("voiced.models.CATALOGUE has no pocket engine")
VOICES_REPO = _POCKET.shared[0].id
MODELS = tuple(m.id for m in _POCKET.models)
"""The Pocket TTS models in the catalogue; the engine refuses any other
``kyutai/pocket-tts*`` or ``mlx-community/pocket-tts*`` id."""
TOKENIZER = "tokenizer.model"
"""The sentencepiece tokenizer in :data:`VOICES_REPO`, the file the model's
``config.json`` names on the Hub."""

# The voices of VOICES_REPO's embeddings/ (the format mlx-audio 0.5.9 reads)
# whose source recordings allow commercial use, per kyutai/tts-voices: name,
# gender, licence of the recording.
_CATALOGUE: tuple[tuple[str, Literal["female", "male"], str], ...] = (
    ("alba", "female", "CC-BY-4.0"),  # alba-mackenna, Alba MacKenna
    ("marius", "male", "CC0-1.0"),  # voice-donations
    ("javert", "male", "CC0-1.0"),  # voice-donations
    ("fantine", "female", "CC-BY-4.0"),  # VCTK p244
    ("eponine", "female", "CC-BY-4.0"),  # VCTK p262
    ("azelma", "female", "CC-BY-4.0"),  # VCTK p303
)

VOICE_FILES: dict[str, str] = {name: f"embeddings/{name}.safetensors" for name, _, _ in _CATALOGUE}
"""Each voice's embedding in :data:`VOICES_REPO`."""

POCKET_VOICES: list[Voice] = [
    Voice(id=name, name=name.capitalize(), language="en", gender=gender)
    for name, gender, _ in _CATALOGUE
]
DEFAULT_VOICE = POCKET_VOICES[0]
"""``alba``, Kyutai's and mlx-audio's default; first, so an engine switch
without a voice picks it."""


class PocketEngine(SentenceTTSEngine):
    name = "pocket"
    model_id = default_model("pocket").id
    languages = ["en"]
    license = default_model("pocket").license

    @classmethod
    def catalogue_voices(cls) -> list[Voice]:
        return list(POCKET_VOICES)

    def __init__(self, model_id: str | None = None) -> None:
        super().__init__(model_id)
        if self.model_id.startswith(("kyutai/pocket-tts", "mlx-community/pocket-tts")) and (
            self.model_id not in MODELS
        ):
            raise ModelRefused(
                f"{self.model_id} is not in voiced's model catalogue; its Pocket TTS models "
                f"are {', '.join(MODELS)}"
            )
        self._prompts: dict[str, Any] = {}
        self._busy = threading.Lock()

    def _load_sync(self) -> None:
        """Build in locals and assign ``_model`` last, so a failure part-way
        leaves the engine unloaded."""
        import mlx.core as mx
        from mlx_audio.tts.models.pocket_tts.pocket_tts import Model

        from voiced.models import snapshot

        started = time.perf_counter()
        root = snapshot(self.model_id, check_only=True)
        voices = snapshot(VOICES_REPO, check_only=True)
        config = json.loads((root / "config.json").read_text())
        # The config names the tokenizer on the Hub; read the pinned copy.
        config["flow_lm"]["lookup_table"]["tokenizer_path"] = str(voices / TOKENIZER)
        model = Model(config)
        weights = cast("dict[str, Any]", mx.load(str(root / "model.safetensors")))
        model.load_weights(list(weights.items()))
        mx.eval(model.parameters())
        model.eval()
        rate = int(model.sample_rate)
        if rate != TTS_SAMPLE_RATE:
            raise RuntimeError(f"Pocket TTS model reports {rate} Hz, expected {TTS_SAMPLE_RATE}")
        prompts = {
            name: cast("dict[str, Any]", mx.load(str(voices / path)))["audio_prompt"]
            for name, path in VOICE_FILES.items()
        }
        # mx.load is lazy: read every voice now, so a bad file fails the load.
        mx.eval(*prompts.values())
        b"".join(_stream(model, prompts[DEFAULT_VOICE.id], WARMUP_TEXT, self._busy))
        self._prompts = prompts
        self._model = model
        log.info(
            "pocket: loaded %s with %d voices in %.1fs",
            self.model_id,
            len(prompts),
            time.perf_counter() - started,
        )

    async def voices(self) -> list[Voice]:
        self._check_loaded()
        return list(POCKET_VOICES)

    def _has_voice(self, voice: str) -> bool:
        return voice in VOICE_FILES

    def _synth_sentence(self, sentence: str, voice: str, speed: float) -> bytes:
        raise NotImplementedError("Pocket TTS streams each sentence: see _synth_chunks")

    def _synth_chunks(
        self, sentence: str, voice: str, speed: float
    ) -> Generator[bytes, None, None]:
        """Blocking; each step runs on the model thread. ``speed`` is ignored:
        the model has no rate control. A sentence without a letter or a digit
        is skipped: the model reads "...", "!!!" or "?" as 0.4 to 0.8 s of sound."""
        if any(c.isalnum() for c in sentence):
            yield from _stream(self._model, self._prompts[voice], sentence, self._busy)


def _stream(
    model: Any, prompt: Any, sentence: str, busy: threading.Lock
) -> Generator[bytes, None, None]:
    """One sentence as 24 kHz int16 PCM, one 80 ms frame at a time. Before
    each frame it waits for STT encoder calls (ADR 0019). Closing the
    generator stops the sentence at the next frame.

    Mimi's streaming decoder state lives on ``model`` and is reset when a
    sentence starts, so two sentences at once would garble each other. The
    exclusive TTS slot rules that out; ``busy`` is held from the first frame
    to the close, and a second sentence started meanwhile raises instead.
    It cannot wait: every step runs on the one model thread."""
    if not busy.acquire(blocking=False):
        raise RuntimeError("Pocket TTS generates one sentence at a time")
    frames: Any = None
    try:
        yielder = gpu.Yielder()
        frames = model.generate_audio_stream(_voice_state(model, prompt), sentence)
        while True:
            yielder()
            frame = next(frames, None)
            if frame is None:
                return
            yield float32_to_pcm16(np.asarray(frame, dtype=np.float32))
    finally:
        if frames is not None:
            frames.close()
        busy.release()


def _voice_state(model: Any, prompt: Any) -> dict[str, Any]:
    """A fresh model state conditioned on a voice embedding, as mlx-audio's
    ``get_state_for_audio_prompt`` builds it for its own voice names (which it
    would fetch from the Hub). A state is used up by one sentence: generating
    moves its cache past the prompt."""
    state: dict[str, Any] = model.init_state()
    model._run_flow_lm_and_increment_step(model_state=state, audio_conditioning=prompt)
    model._slice_flow_cache(state, prompt.shape[1])
    return state
