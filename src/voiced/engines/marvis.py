"""Marvis TTS on the GPU through mlx-audio. Selectable, not the default.

Marvis is a small model of Sesame's CSM design: a backbone transformer
predicts each 80 ms frame's first Mimi codebook from the text and the audio
so far, a decoder transformer the other 31, and Mimi's decoder turns the
frames into 24 kHz audio. No phonemizer. A voice is a reference clip and its
transcript: the model continues the clip's audio with the new text. This
engine (see ``docs/decisions/0031-tts-marvis.md``):

* runs its own loop over mlx-audio's ``sesame`` model rather than its
  ``generate``: the same steps, with the voice clip encoded once at load
  rather than for every sentence, each frame decoded and yielded as soon as
  it is sampled (first audio is the first frame), no progress bar, and a wait
  for any speech-to-text encoder call that holds the GPU before every frame
  (``voiced.gpu``, ADR 0019);
* bounds each sentence's frames by its length (``sentence_bound``), so a
  runaway stops;
* relies on the sentence loop running the load and every step on the
  engine's one model thread (``voiced.engines.sentence``): the attention
  caches and Mimi's streaming state are MLX arrays kept from frame to frame;
* generates one sentence at a time: those caches live on the model, so an
  overlapping second sentence is refused, not garbled;
* loads the model, the tokenizer, the voices and Mimi from voiced's pinned
  snapshots (``voiced.models``, the catalogue), as local files, so mlx-audio
  never reaches the Hub itself (:func:`_pinned_mimi`).

The voices are the four reference clips Marvis publishes in its Apache-2.0
repository, scripted sentences read for it: two English, one French, one
German. Each speaks its own language; ``conversational_a`` is the default.
mlx-audio applies CSM's watermark only when ``silentcipher`` is installed,
which voiced does not install, so the audio is not watermarked. Marvis has no
rate control: ``speed`` is checked against the shared bounds and otherwise
ignored.

Everything that imports MLX or mlx-audio runs inside ``load()``, so this
module imports (the entry-point registry does so) without the ``tts`` extra.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Generator, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import numpy as np

from voiced import gpu
from voiced.audio import float32_to_pcm16
from voiced.engines.base import TTS_SAMPLE_RATE, Voice
from voiced.engines.registry import ModelRefused
from voiced.engines.sentence import WARMUP_TEXT, SentenceTTSEngine, sentence_bound
from voiced.models import default_model, engine

log = logging.getLogger(__name__)

__all__ = ["DEFAULT_VOICE", "MARVIS_VOICES", "MODELS", "MarvisEngine"]

_MARVIS = engine("marvis")
if _MARVIS is None:  # pragma: no cover - the catalogue ships with the engine
    raise ImportError("voiced.models.CATALOGUE has no marvis engine")
MODELS = tuple(m.id for m in _MARVIS.models)
"""The conversions in the catalogue; the engine refuses any other
``Marvis-AI/`` id."""
VOICES_REPO = _MARVIS.shared[0].id
"""The text tokenizer and the reference clips (the 250M's full repository)."""
MIMI_REPO = _MARVIS.shared[1].id
MIMI_FILE = _MARVIS.shared[1].patterns[0]

FRAME_SECONDS = 0.08
"""One Mimi frame: 1920 samples at 24 kHz."""
TEMPERATURE = 0.9
TOP_K = 50
"""mlx-audio's sampling for the sesame model."""
MAX_SEQUENCE = 2048
"""The backbone's context: the voice's transcript and the sentence's text, the
voice's frames, then every frame of the sentence. The splitter's longest
sentence (300 characters) reaches about 1600 of it: 166 clip frames, at most
463 sentence frames, and the text tokens."""
FADE_SAMPLES = 240
"""10 ms faded out at the end of a sentence the bound cuts, so it does not
stop on a click."""

# The reference clips (prompts/<id>.wav and .txt in VOICES_REPO): id, name,
# language, gender (from the clip's pitch: about 180 Hz for a, 105 to 120 Hz
# for the others).
_CATALOGUE: tuple[tuple[str, str, str, Literal["female", "male"]], ...] = (
    ("conversational_a", "Conversational A", "en", "female"),
    ("conversational_b", "Conversational B", "en", "male"),
    ("conversational_fr", "Conversational FR", "fr", "male"),
    ("conversational_de", "Conversational DE", "de", "male"),
)

MARVIS_VOICES: list[Voice] = [
    Voice(id=vid, name=name, language=language, gender=gender)
    for vid, name, language, gender in _CATALOGUE
]
DEFAULT_VOICE = MARVIS_VOICES[0]
"""``conversational_a``, mlx-audio's default; first, so an engine switch
without a voice picks it."""


@dataclass(frozen=True)
class _Prompt:
    """A voice as the model reads it: the clip's transcript, which each
    sentence's text follows, and the clip's Mimi frames with their mask,
    encoded once at load."""

    text: str
    tokens: Any
    mask: Any


class MarvisEngine(SentenceTTSEngine):
    name = "marvis"
    model_id = default_model("marvis").id
    languages = ["en", "fr", "de"]
    license = default_model("marvis").license

    @classmethod
    def catalogue_voices(cls) -> list[Voice]:
        return list(MARVIS_VOICES)

    def __init__(self, model_id: str | None = None) -> None:
        super().__init__(model_id)
        if self.model_id.startswith("Marvis-AI/") and self.model_id not in MODELS:
            raise ModelRefused(
                f"{self.model_id} is not in voiced's model catalogue; its Marvis models "
                f"are {', '.join(MODELS)}"
            )
        self._prompts: dict[str, _Prompt] = {}
        self._busy = threading.Lock()

    def _unload_sync(self) -> None:
        self._model = None
        self._prompts = {}

    def _load_sync(self) -> None:
        """Build in locals and assign ``_model`` last, so a failure part-way
        leaves the engine unloaded. The steps are mlx-audio's ``load_model``
        for this model, with the tokenizer and Mimi read from the pins."""
        import mlx.core as mx
        from mlx_audio.tts.models.sesame.sesame import Model
        from mlx_audio.utils import apply_quantization, load_weights

        from voiced.models import snapshot

        started = time.perf_counter()
        root = snapshot(self.model_id, check_only=True)
        voices = snapshot(VOICES_REPO, check_only=True)
        mimi = snapshot(MIMI_REPO, check_only=True) / MIMI_FILE
        config = json.loads((root / "config.json").read_text())
        # The config names the tokenizer's repository on the Hub; read the pinned copy.
        config["text_tokenizer"] = str(voices)
        with _pinned_mimi(mimi):
            model = Model(config)
        weights = model.sanitize(load_weights(root))
        apply_quantization(model, config, weights, model.model_quant_predicate)
        model.load_weights(list(weights.items()), strict=True)
        mx.eval(model.parameters())
        model.eval()
        rate = int(model.sample_rate)
        if rate != TTS_SAMPLE_RATE:
            raise RuntimeError(f"Marvis model reports {rate} Hz, expected {TTS_SAMPLE_RATE}")
        prompts = {v.id: _prompt(model, voices / "prompts", v.id) for v in MARVIS_VOICES}
        _drop_mimi_encoder(model._audio_tokenizer)
        for _ in _stream(model, prompts[DEFAULT_VOICE.id], WARMUP_TEXT, self._busy):
            pass
        self._prompts = prompts
        self._model = model
        log.info(
            "marvis: loaded %s with %d voices in %.1fs",
            self.model_id,
            len(prompts),
            time.perf_counter() - started,
        )

    async def voices(self) -> list[Voice]:
        self._check_loaded()
        return list(MARVIS_VOICES)

    def _has_voice(self, voice: str) -> bool:
        return voice in self._prompts

    def _synth_sentence(self, sentence: str, voice: str, speed: float) -> bytes:
        raise NotImplementedError("Marvis streams each sentence: see _synth_chunks")

    def _synth_chunks(
        self, sentence: str, voice: str, speed: float
    ) -> Generator[bytes, None, None]:
        """Blocking; each step runs on the model thread. ``speed`` is ignored:
        the model has no rate control. A sentence without a letter or a digit
        is skipped: there is nothing in it to read."""
        if any(c.isalnum() for c in sentence):
            for frame in _stream(self._model, self._prompts[voice], sentence, self._busy):
                yield float32_to_pcm16(frame)


# -- blocking helpers ----------------------------------------------------------


_patch_lock = threading.Lock()
"""Serializes :func:`_pinned_mimi`, so overlapping loads cannot leave the
wrapper installed."""


@contextmanager
def _pinned_mimi(path: Path) -> Iterator[None]:
    """Hand mlx-audio the pinned Mimi weights while the model is built.

    The sesame ``Model`` builds Mimi with ``Mimi.from_pretrained(MIMI_REPO)``
    (``mlx_audio/tts/models/sesame/sesame.py``, line 474 in 0.5.9), which
    calls ``hf_hub_download`` with no revision; it offers no argument to name
    a file or a revision. The services load with ``HF_HUB_OFFLINE``, where an
    unpinned lookup of a repository downloaded at a pin fails, and ``voiced
    install`` would fetch ``main``. For the build, the codec module's
    ``hf_hub_download`` returns ``path`` for that one file and refuses any
    other, and it is restored when the build ends.
    """
    from mlx_audio.codec.models.mimi import mimi

    with _patch_lock:
        original = mimi.hf_hub_download

        def download(repo_id: str, filename: str, *args: Any, **kwargs: Any) -> str:
            if (repo_id, filename) != (MIMI_REPO, MIMI_FILE):
                raise RuntimeError(f"marvis: unexpected download of {repo_id}/{filename}")
            return str(path)

        mimi.hf_hub_download = download
        try:
            yield
        finally:
            mimi.hf_hub_download = original


def _prompt(model: Any, folder: Path, voice: str) -> _Prompt:
    """Voice ``voice``'s clip and transcript, the clip encoded into Mimi
    frames without an end frame, so the model continues it."""
    import mlx.core as mx

    text = (folder / f"{voice}.txt").read_text().strip()
    segment = model.prepare_prompt(text, 0, str(folder / f"{voice}.wav"), TTS_SAMPLE_RATE)
    tokens, mask = model._tokenize_audio(segment.audio, add_eos=False)
    mx.eval(tokens, mask)
    return _Prompt(text=text, tokens=tokens, mask=mask)


def _drop_mimi_encoder(mimi: Any) -> None:
    """Free Mimi's encoder (about 155 MB of float32 weights), which only
    :func:`_prompt` uses, once every voice is encoded. Decoding reads the
    quantizer, ``upsample``, ``decoder_transformer``, ``decoder`` and
    ``decoder_cache`` (``Mimi.decode_step`` and ``MimiStreamingDecoder``)."""
    import mlx.core as mx

    for part in ("encoder", "encoder_transformer", "downsample"):
        delattr(mimi, part)
    mimi.encoder_cache = []
    mx.clear_cache()


def max_frames(sentence: str) -> int:
    """The most frames ``sentence`` may take: :func:`sentence_bound` in 80 ms frames."""
    return sentence_bound(sentence, FRAME_SECONDS).tokens


def _stream(model: Any, prompt: _Prompt, sentence: str, busy: threading.Lock) -> Iterator[Any]:
    """One sentence as 24 kHz float32 frames of 80 ms, as mlx-audio's
    ``generate`` makes them with ``voice_match``: the voice's transcript and
    the sentence as one text, then the voice's frames, which the model
    continues until it samples an all-zero frame, or until
    :func:`max_frames`, where the last frame is faded out. Before each frame
    it waits for STT encoder calls (ADR 0019). Closing the generator stops
    the sentence at the next frame. Compare it with ``Model.generate`` in
    ``mlx_audio/tts/models/sesame/sesame.py`` (lines 774 to 850 in 0.5.9)
    when mlx-audio is bumped.

    The model's attention caches and Mimi's streaming decoder are reset when
    a sentence starts, so two sentences at once would garble each other. The
    exclusive TTS slot rules that out; ``busy`` is held from the first frame
    to the close, and a second sentence started meanwhile raises instead.
    It cannot wait: every step runs on the one model thread."""
    import mlx.core as mx
    from mlx_audio.lm.sample_utils import make_sampler

    if not busy.acquire(blocking=False):
        raise RuntimeError("Marvis generates one sentence at a time")
    try:
        text, text_mask = model._tokenize_text_segment(f"{prompt.text} {sentence}", 0)
        tokens = mx.concat([text, prompt.tokens], axis=0).astype(mx.int32)[None]
        mask = mx.concat([text_mask, prompt.mask], axis=0).astype(mx.bool_)[None]
        limit = max_frames(sentence)
        if tokens.shape[1] + limit > MAX_SEQUENCE:
            raise RuntimeError(f"marvis: sentence too long for the model ({len(sentence)} chars)")
        position = mx.arange(tokens.shape[1], dtype=mx.int32)[None]
        sampler = make_sampler(temp=TEMPERATURE, top_k=TOP_K)
        model.model.reset_caches()
        model._streaming_decoder.reset()
        yielder = gpu.Yielder()
        for step in range(1, limit + 1):
            yielder()
            frame = model.model.generate_frame(tokens, mask, position, sampler)
            if mx.all(frame == 0).item():
                return
            audio = model._streaming_decoder.decode_frames(frame[:, :, None])
            samples = np.array(audio, dtype=np.float32).reshape(-1)
            if step == limit:
                samples[-FADE_SAMPLES:] *= np.linspace(1.0, 0.0, FADE_SAMPLES, dtype=np.float32)
            yield samples
            # The next input: this frame's codebooks and an empty text slot.
            tokens = mx.concat([frame, mx.zeros((1, 1), dtype=mx.int32)], axis=1)[:, None]
            mask = mx.concat(
                [mx.ones_like(frame).astype(mx.bool_), mx.zeros((1, 1), dtype=mx.bool_)], axis=1
            )[:, None]
            position = position[:, -1:] + 1
        log.warning(
            "marvis: runaway generation for %r: cut at the %d-frame length bound",
            sentence[:60],
            limit,
        )
    finally:
        busy.release()
