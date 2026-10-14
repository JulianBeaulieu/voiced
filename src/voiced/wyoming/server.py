"""Wyoming listeners. Both run in the same process and event loop as the HTTP
app for their modality and share its ``ServiceContext`` (engine, queue, health).

Planned (HANDOFF section 5):

* STT: ``Describe`` -> ``Info`` with the model and languages; ``AudioStart``,
  ``AudioChunk``, ``AudioStop`` fed into an ``STTStreamSession`` inside
  ``ctx.queue.slot()``, returning ``Transcript``.
* TTS: ``Describe`` -> ``Info`` listing voices; ``Synthesize`` and the
  streaming ``SynthesizeStart`` / ``SynthesizeChunk`` / ``SynthesizeStop``
  (HA >= 2025.7) driven by ``TTSEngine.synthesize``.
* Zeroconf advertisement of ``voiced-stt`` and ``voiced-tts`` on the
  ``_wyoming._tcp.local.`` service type.
"""

from __future__ import annotations

from voiced.api.app import ServiceContext


async def serve_stt(ctx: ServiceContext, host: str, port: int) -> None:
    raise NotImplementedError("Wyoming STT listener is not implemented yet")


async def serve_tts(ctx: ServiceContext, host: str, port: int) -> None:
    raise NotImplementedError("Wyoming TTS listener is not implemented yet")
