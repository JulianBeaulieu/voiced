"""OmniVoice on MLX. Selectable, not the default: the language-coverage engine.

OmniVoice (k2-fsa) is a masked-diffusion text-to-speech model: a
bidirectional Qwen3-0.6B backbone fills in a whole sentence's audio tokens
over :data:`NUM_STEPS` refinement steps, and the HiggsAudio v2 codec decodes
them to 24 kHz audio. Its model card tags 646 languages. This engine (see
``docs/decisions/0030-tts-omnivoice.md``):

* runs mlx-audio's port of the model on the GPU (no PyTorch), the weights
  cast to float16 at load, and yields to the STT service before every
  forward pass (ADR 0019);
* cannot stream within a sentence: audio exists only after the last step,
  so first audio is the whole first sentence, about 0.8 s on an M1 Max
  against the 200 ms target. It is the engine for languages Kokoro does not
  speak, not a real-time one;
* stops a sentence at its next forward pass when the client leaves;
* loads the model pinned in ``voiced.models``, never downloading it;
* trims the model's edge silence and levels each sentence, as upstream's
  post-processing does.

Voices are designed from attributes (gender, age, accent): a fixed table of
:data:`OMNIVOICE_VOICES`, each one language and one ``instruct`` string
pinned here. The model's language tag is the voice's primary subtag, so
``apc`` (Levantine Arabic) says ``apc`` to the model. ``speed`` scales the
length the model is asked to fill, as upstream's ``speed`` does.

Everything that imports MLX runs inside ``load()``, so this module imports
(the entry-point registry does so) without the ``tts`` extra.
"""

from __future__ import annotations

import functools
import logging
import threading
import time
from collections.abc import Generator
from dataclasses import dataclass
from typing import Any

import numpy as np
from numpy.typing import NDArray

from voiced import gpu
from voiced.audio import float32_to_pcm16
from voiced.engines.base import TTS_SAMPLE_RATE, Voice
from voiced.engines.registry import ModelRefused
from voiced.engines.sentence import WARMUP_TEXT, SentenceTTSEngine
from voiced.models import default_model
from voiced.text import primary_subtag

log = logging.getLogger(__name__)

__all__ = ["DESIGNED", "LANGUAGES", "OMNIVOICE_VOICES", "OmniVoiceEngine"]

NUM_STEPS = 16
"""Refinement steps per sentence. Upstream's default is 32; 16, which its
docs name for faster synthesis, halves the time per sentence, and its
speech transcribed the same in all six voices' languages (ADR 0030)."""
SEED = 1234
"""Each sentence is generated from the same random state, so the same text
in the same voice gives the same audio."""
FRAMES_PER_SECOND = 25
"""Audio tokens per second of speech (24 kHz, 960 samples a frame)."""
MIN_FRAMES = 10
LENGTH_MARGIN = 1.15
"""The length the model fills is the rule-based estimate times this, at
least :data:`MIN_FRAMES`: mlx-audio's own choice."""
SILENCE_DBFS = -50.0
EDGE_SILENCE_S = 0.1
"""Edge silence quieter than :data:`SILENCE_DBFS` (in 10 ms windows) is cut
to this much at each end, as upstream's post-processing does; the model
fills its estimated length, so a sentence often starts with half a second of
silence."""
PEAK = 0.5
"""Each sentence is scaled to this peak, as upstream does."""

LANGUAGES: list[str] = """
aae aal aao ab abb abn abr abs abv acm acw acx adf adx ady aeb aec af afb afo ahl
ahs ajg aju ala aln alo am amu an anc ank anp anw aom apc apd arb arq ars ary arz as
ast avl awo ayl ayp az ba bag bas bax bba bbj bbl bbu bce bci bcs bcy bda bde bdm be
beb bew bfd bft bg bgp bhb bhh bho bhp bhr bjj bjk bjn bjt bkh bkm bky bmm bmq bn
bnm bnn bns bo bou bqg br bra brh bri brx bs bsh bsj bsk btm btv bug bum buo bux bwr
bxf byc bys byv byx bzc bzw ca ccg ceb cen cfa cgg chq cjk ckb ckl ckr cky cnh cpy
cs cte ctl cut cux cv cy da dag dar dav dbd dcc de deg dgh dgo dje dmk dml dru dty
dua dv dyu dzg ebr ebu ego eiv eko ekr el elm en eo es esu et eto ets etu eu ewo ext
eyo fa fan fat ff ffm fi fia fil fip fkk fmp fr fub fuc fue fuf fuh fui fuq fuv fy
ga gbm gbr gby gcc gdf gej ges ggg gid gig giz gjk gju gl glw gn gol gom gsl gu gui
gur guz gv gwc gwe gwt gya gyz ha hah hao haw haz hbb he hem hi hia hkk hla hno hoj
hr hsb ht hu hue hul hux hwo hy hz ia ibb id ida idu ig ijc ijn ik ikw is ish iso it
its itw itz ja jal jax jgo jmx jns jqr juk juo jv ka kab kai kaj kam kbd kbl kbt kcq
kdh kea keu kfe kfk kfp khg khw kj kjc kjk kk kln kls km kmr kmy kn kna knn ko kol
koo kpo kqo ks ksd ksf kto kuh kvx kw kwm kxp ky kyx lag lb lcm ldb lg lij lir lkb
lla ln lnu lo loa lrk lss lt ltg lto lua luo lus lv lwg mab maf mai mau max mbo mcf
mcn mcx mdd mde mdf mek mer meu mfm mfn mfo mfv mgg mgi mhk mhr mi mig miu mk mkf
mki ml mlq mn mne mni mqy mr mrj mrr mrt ms mse msh msw mt mtr mtu mtx mua mug mui
mve mvy mxs mxu mxy my myv mzl nal nan nap nb nbh ncf nco ncx ndi ng ngi nhg nhi nhn
nhq nja nl nla nlv nmg nmz nn nnh no noe npi nso ny nyu oc odk odu ogo om orc oru
ory os pa pbs pbt pbu pcm pex phl phr pip piy pko pl plk plt pmq pms pmy pnb poc poe
pow prq ps pst pt pua pwn qug qum qup qur qus quv qux quy qva qvi qvj qvl qwa qws
qxa qxp qxt qxu qxw rag rm ro rob rof roo rth ru rup rw sa sah sat sau say sbn sc
scl scn sd sei shu si sip siw sjr sk skg skr sl sn snc snk so sol sps sq sr src sro
ssi ste sua sv sva sw szy ta tan tar tay tbf tcf tcy tdn tdx te tg tgc th the thq
thr thv ti tig tio tk tkg tkt tli tlp tn tok tpl tpz tqp tr trp trq trv trw tt ttj
ttr ttu tui tul tuq tuv tuy tvo tvu tw twu txs txy udl ug uk uki umb ur ush uz uzn
vai var ver vi vmc vmj vmm vmp vmz vot vro wbl wci weo wes wja wji wo wof xh xhe xka
xmf xmv xmw xpe xti xtu yaq yav yay ydd ydg yer yes yi yo yue zga zgh zh zoc zoh
zor zpv zpy ztg ztn ztp zts ztu zu zza
""".split()
"""The ``language`` tags on the model card at the pinned revision, in its
order (the card's YAML quotes ``no`` and ``yes``; the tags are without
them): what the model was trained to speak. A service serves those of them
``tts.languages`` names."""


@dataclass(frozen=True)
class DesignedVoice:
    voice: Voice
    instruct: str
    """Upstream's voice-design attributes, as upstream normalizes them for
    text in the voice's language (Chinese attributes for Chinese text)."""

    @property
    def tag(self) -> str:
        """The model's language tag: the voice's primary subtag."""
        return primary_subtag(self.voice.language)


# A voice's name is its age attribute and its gender its gender attribute:
# keep them in step with ``instruct``. First: an engine switch without a
# voice picks it.
DESIGNED: tuple[DesignedVoice, ...] = (
    DesignedVoice(
        Voice(id="en-US-female", name="Young adult", language="en-US", gender="female"),
        "female, young adult, american accent",
    ),
    DesignedVoice(
        Voice(id="de-DE-male", name="Middle-aged", language="de-DE", gender="male"),
        "male, middle-aged",
    ),
    DesignedVoice(
        Voice(id="apc-female", name="Young adult", language="apc", gender="female"),
        "female, young adult",
    ),
    DesignedVoice(
        Voice(id="es-ES-male", name="Young adult", language="es-ES", gender="male"),
        "male, young adult",
    ),
    DesignedVoice(
        Voice(id="fr-FR-female", name="Middle-aged", language="fr-FR", gender="female"),
        "female, middle-aged",
    ),
    DesignedVoice(
        Voice(id="zh-CN-male", name="Young adult", language="zh-CN", gender="male"),
        "男，青年",  # noqa: RUF001 (upstream's form for Chinese text)
    ),
)
_BY_ID = {d.voice.id: d for d in DESIGNED}

OMNIVOICE_VOICES: list[Voice] = [d.voice for d in DESIGNED]


class OmniVoiceEngine(SentenceTTSEngine):
    name = "omnivoice"
    model_id = default_model("omnivoice").id
    languages = LANGUAGES
    license = default_model("omnivoice").license

    @classmethod
    def catalogue_voices(cls) -> list[Voice]:
        return list(OMNIVOICE_VOICES)

    def __init__(self, model_id: str | None = None) -> None:
        super().__init__(model_id)
        self._stop = threading.Event()
        if self.model_id != OmniVoiceEngine.model_id:
            # The mlx-community conversions among them: a third party's copy
            # under a wrong licence tag (ADR 0030).
            raise ModelRefused(
                f"{self.model_id} is not in voiced's model catalogue; OmniVoice's model "
                f"is {OmniVoiceEngine.model_id}"
            )

    def _load_sync(self) -> None:
        """Build in locals and assign ``_model`` last, so a failure part-way
        leaves the engine unloaded."""
        from voiced.models import snapshot

        started = time.perf_counter()
        model = _load_model(str(snapshot(self.model_id, check_only=True)))
        rate = int(model.sample_rate)
        if rate != TTS_SAMPLE_RATE:
            raise RuntimeError(f"OmniVoice model reports {rate} Hz, expected {TTS_SAMPLE_RATE}")
        _synth(model, WARMUP_TEXT, DESIGNED[0], 1.0, threading.Event())
        self._model = model
        log.info(
            "omnivoice: loaded %s (%d steps) in %.1fs",
            self.model_id,
            NUM_STEPS,
            time.perf_counter() - started,
        )

    async def voices(self) -> list[Voice]:
        self._check_loaded()
        return list(OMNIVOICE_VOICES)

    def _has_voice(self, voice: str) -> bool:
        return voice in _BY_ID

    def _synth_sentence(self, sentence: str, voice: str, speed: float) -> bytes:
        raise NotImplementedError("OmniVoice can stop within a sentence: see _synth_chunks")

    def _synth_chunks(
        self, sentence: str, voice: str, speed: float
    ) -> Generator[bytes, None, None]:
        """Not a generator itself: the sentence loop calls it on the event
        loop, so the sentence's stop event is in place before its step is
        submitted, and a cancel while the step is still queued stops it too."""
        self._stop = stop = threading.Event()
        return _sentence(self._model, sentence, _BY_ID[voice], speed, stop)

    def _interrupt(self) -> None:
        """The client left mid-sentence: the next forward pass stops it, so
        the GPU is free within a pass instead of after the whole sentence."""
        self._stop.set()


# -- blocking helpers ----------------------------------------------------------


def _sentence(
    model: Any, sentence: str, voice: DesignedVoice, speed: float, stop: threading.Event
) -> Generator[bytes, None, None]:
    """Blocking; runs on the model thread. A sentence without a letter or a
    digit is skipped: the model fills at least 0.4 s with sound. A stopped
    sentence gives nothing, which the cancelled request discards anyway."""
    if not any(c.isalnum() for c in sentence):
        return
    try:
        yield _synth(model, sentence, voice, speed, stop)
    except _Stopped:
        return


def _load_model(path: str) -> Any:
    """mlx-audio's OmniVoice with its tokenizers, every weight in float16
    (the checkpoint is float32)."""
    import mlx.core as mx
    from mlx.utils import tree_map
    from mlx_audio.tts.utils import load_model

    model = load_model(path, lazy=True)  # cast before the float32 weights are read
    model.update(
        tree_map(
            lambda p: p.astype(mx.float16) if mx.issubdtype(p.dtype, mx.floating) else p,
            model.parameters(),
        )
    )
    mx.eval(model.parameters())
    return model


@functools.cache
def _estimator() -> Any:
    from mlx_audio.tts.models.omnivoice.duration import RuleDurationEstimator

    return RuleDurationEstimator()


def _frames(text: str) -> float:
    """mlx-audio's rule-based length of ``text`` in frames, against its
    reference of 25 frames for "Nice to meet you."."""
    return float(_estimator().estimate_duration(text, "Nice to meet you.", FRAMES_PER_SECOND))


class _Stopped(Exception):
    """The sentence's stop event was set: nobody is listening."""


class _Yielding:
    """The model as ``iterative_unmask`` calls it, waiting for STT encoder
    calls before each forward pass and running each pass on its own, so an
    STT call waits behind at most one pass (about 35 ms), not a step. Once
    ``stop`` is set, the next pass raises :class:`_Stopped` instead."""

    def __init__(self, model: Any, yielder: gpu.Yielder, stop: threading.Event) -> None:
        self.config = model.config
        self._model = model
        self._yielder = yielder
        self._stop = stop

    def __call__(self, input_ids: Any, audio_mask: Any) -> Any:
        import mlx.core as mx

        self._yielder()
        if self._stop.is_set():
            raise _Stopped
        logits = self._model(input_ids, audio_mask)
        mx.eval(logits)
        return logits


def _synth(
    model: Any, sentence: str, voice: DesignedVoice, speed: float, stop: threading.Event
) -> bytes:
    """One sentence to 24 kHz int16 PCM: mlx-audio's ``generate`` without its
    voice cloning, with ``speed``, a GPU yield point per forward pass and
    ``stop`` checked before each. Raises :class:`_Stopped` once stopped."""
    import mlx.core as mx
    from mlx_audio.tts.models.omnivoice.generation import iterative_unmask

    length = frames_to_fill(_frames(sentence), speed)
    style_ids, text_ids = model._tokenize_style_and_text(
        text=sentence,
        language=voice.tag,
        instruct=voice.instruct,
        text_tokenizer=model.text_tokenizer,
        denoise=False,
    )
    inputs = model._prepare_inference_inputs(style_ids, text_ids, length)
    yielder = gpu.Yielder()
    mx.random.seed(SEED)
    tokens = iterative_unmask(
        _Yielding(model, yielder, stop),
        cond_input_ids=inputs["input_ids"],
        cond_audio_mask=inputs["audio_mask"],
        T=length,
        num_steps=NUM_STEPS,
    )
    yielder()
    audio = np.asarray(model.audio_tokenizer.decode(tokens).astype(mx.float32)).reshape(-1)
    out = tidy(audio, TTS_SAMPLE_RATE)
    if len(out) == 0:
        # Silence, or float16 overflowing to NaN: say so, not just skip it.
        log.warning("omnivoice: no audio for a sentence of %d characters", len(sentence))
    return float32_to_pcm16(out)


def frames_to_fill(estimate: float, speed: float) -> int:
    """The audio frames the model fills for a sentence estimated at
    ``estimate`` frames: mlx-audio's rule, divided by ``speed`` as upstream
    divides its estimate."""
    return max(MIN_FRAMES, int(estimate * LENGTH_MARGIN / speed))


def tidy(audio: NDArray[np.float32], rate: int) -> NDArray[np.float32]:
    """Cut edge silence to :data:`EDGE_SILENCE_S` and scale to :data:`PEAK`:
    upstream's post-processing (``remove_silence_edges`` at
    :data:`SILENCE_DBFS` over 10 ms windows, then the peak), in numpy.
    All silence is nothing."""
    window = rate // 100
    n = len(audio) // window
    if n == 0:
        return audio[:0]
    rms = np.sqrt(np.mean(np.square(audio[: n * window].reshape(n, window)), axis=1))
    loud = np.flatnonzero(rms > 10 ** (SILENCE_DBFS / 20))
    if len(loud) == 0:
        return audio[:0]
    keep = int(EDGE_SILENCE_S * rate)
    start = max(0, loud[0] * window - keep)
    end = min(len(audio), (loud[-1] + 1) * window + keep)
    out = audio[start:end]
    peak = float(np.abs(out).max())
    return (out * (PEAK / peak)).astype(np.float32)
