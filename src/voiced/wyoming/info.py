"""``Info`` messages for both listeners.

Home Assistant reads these fields (``homeassistant/components/wyoming``):

* The config entry title and entity names come from the first *installed*
  ``AsrProgram`` / ``TtsProgram`` name. Only ``asr[0]`` is consulted.
* STT languages are the union of the installed ``AsrModel`` languages;
  ``requires_external_vad`` and the ``prefers_*`` flags configure HA's own
  audio pipeline for this provider.
* TTS voices are grouped by language; ``description or name`` is shown to
  the user; ``supports_synthesize_streaming`` enables the text-streaming path.
"""

from __future__ import annotations

from wyoming.info import AsrModel, AsrProgram, Attribution, Info, TtsProgram, TtsVoice

from voiced import __version__
from voiced.context import ServiceContext
from voiced.engines.base import Voice
from voiced.wyoming import WYOMING_ASR_NAME, WYOMING_TTS_NAME

PROJECT_URL = "https://github.com/JulianBeaulieu/voiced"


def _model_attribution(engine_name: str, model_id: str) -> Attribution:
    hosted = "/" in model_id and not model_id.startswith(("/", ".", "~"))
    return Attribution(name=engine_name, url=f"https://huggingface.co/{model_id}" if hosted else "")


def stt_info(ctx: ServiceContext) -> Info:
    engine = ctx.stt_engine
    languages = ctx.languages
    model = AsrModel(
        name=engine.model_id,
        description=f"{engine.name} ({engine.license})",
        attribution=_model_attribution(engine.name, engine.model_id),
        installed=True,
        version=None,
        languages=languages,
    )
    program = AsrProgram(
        name=WYOMING_ASR_NAME,
        description="voiced speech-to-text",
        attribution=Attribution(name="voiced", url=PROJECT_URL),
        installed=True,
        version=__version__,
        models=[model],
        # Home Assistant decides when the utterance ends and sends AudioStop;
        # the listener transcribes exactly the audio it is given.
        requires_external_vad=True,
    )
    return Info(asr=[program])


def tts_voice(voice: Voice, engine_name: str, model_id: str) -> TtsVoice:
    description = voice.name
    if voice.gender:
        description = f"{voice.name} ({voice.gender})"
    return TtsVoice(
        name=voice.id,
        description=description,
        attribution=_model_attribution(engine_name, model_id),
        installed=True,
        version=None,
        languages=[voice.language],
    )


async def tts_info(ctx: ServiceContext) -> Info:
    engine = ctx.tts_engine
    voices = [tts_voice(v, engine.name, engine.model_id) for v in await ctx.voices()]
    program = TtsProgram(
        name=WYOMING_TTS_NAME,
        description=f"voiced text-to-speech ({engine.name}, {engine.model_id})",
        attribution=Attribution(name="voiced", url=PROJECT_URL),
        installed=True,
        version=__version__,
        voices=sorted(voices, key=lambda v: v.name),
        supports_synthesize_streaming=True,
    )
    return Info(tts=[program])
