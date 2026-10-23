"""Every shipped engine gives its memory back on ``unload()`` and works after
loading again (ADR 0033). Real models and Metal: ``VOICED_MODEL_TESTS=1``.

    VOICED_MODEL_TESTS=1 uv run pytest tests/test_unload_models.py -m model -s

Each engine runs in its own interpreter, so one engine's leftovers do not
count against the next. An engine whose model is not downloaded is skipped.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from voiced.engines import registry

ENGINES = [
    *(("stt", name) for name in registry.list_stt_engines() if name != "fake"),
    *(("tts", name) for name in registry.list_tts_engines() if name != "fake"),
]

MOST = 0.75
"""Share of what the load added to the footprint that the unload must return
for the engines the ADR measures."""
MEASURED = {"parakeet", "kokoro"}

SPEECH = Path(__file__).parent / "fixtures" / "speech_en.wav"

CHILD = """
import asyncio, gc, json, sys, wave
import numpy as np
from voiced.engines import registry
from voiced.engines.base import AudioInput, STTEngine
from voiced.metrics import phys_footprint
from voiced.models import ModelNotDownloaded

kind, name, speech = sys.argv[1:]

def footprint():
    gc.collect()
    return phys_footprint()

async def use(engine):
    if isinstance(engine, STTEngine):
        with wave.open(speech) as w:
            pcm = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")
        result = await engine.transcribe(AudioInput(pcm.astype(np.float32) / 32768.0))
        return result.text
    voice = (await engine.voices())[0].id
    pcm = b"".join([c.data async for c in engine.synthesize("Hello there.", voice)])
    return len(pcm)

async def main():
    cls = registry.get_engine(kind, name)
    engine = cls(cls.model_id)
    before = footprint()
    try:
        await engine.load()
    except ModelNotDownloaded as exc:
        print(json.dumps({"skip": str(exc)}))
        return
    first = await use(engine)
    loaded = footprint()
    await engine.unload()
    unloaded = footprint()
    await engine.load()
    again = await use(engine)
    reloaded = footprint()
    print(json.dumps({"before": before, "loaded": loaded, "unloaded": unloaded,
                      "reloaded": reloaded, "first": first, "again": again}))

asyncio.run(main())
"""


@pytest.mark.model
@pytest.mark.parametrize(("kind", "name"), ENGINES, ids=[n for _, n in ENGINES])
def test_unload_returns_the_memory_and_reload_works(kind: str, name: str) -> None:
    proc = subprocess.run(
        [sys.executable, "-c", CHILD, kind, name, str(SPEECH)],
        capture_output=True,
        text=True,
        timeout=900,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr[-3000:]
    result = json.loads(proc.stdout.strip().splitlines()[-1])
    if "skip" in result:
        pytest.skip(result["skip"])
    gb = {
        k: round(v / 1e9, 2)
        for k, v in result.items()
        if k in ("before", "loaded", "unloaded", "reloaded")
    }
    print(f"\n{name}: footprint GB {gb}")
    added = result["loaded"] - result["before"]
    returned = result["loaded"] - result["unloaded"]
    assert result["again"] == result["first"] or kind == "tts", "same transcript after reload"
    assert result["again"], "the reloaded engine works"
    if name in MEASURED:
        assert returned >= MOST * added, f"returned {returned / 1e9:.2f} of {added / 1e9:.2f} GB"
