"""Wyoming protocol listeners for Home Assistant.

See ``voiced.wyoming.server`` for the listener and ``docs/decisions/0004-wyoming.md``
for the design. Service identifiers are fixed here so the HA integration, zeroconf
advertisement and docs all agree.
"""

WYOMING_STT_SERVICE = "voiced-stt"
WYOMING_TTS_SERVICE = "voiced-tts"
WYOMING_ASR_NAME = "voiced"
WYOMING_TTS_NAME = "voiced"
ZEROCONF_TYPE = "_wyoming._tcp.local."

__all__ = [
    "WYOMING_ASR_NAME",
    "WYOMING_STT_SERVICE",
    "WYOMING_TTS_NAME",
    "WYOMING_TTS_SERVICE",
    "ZEROCONF_TYPE",
]
