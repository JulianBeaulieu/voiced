"""Engine catalogues: known without loading weights, and never loaded in the watchdog."""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from voiced.engines.base import Voice
from voiced.engines.catalogue import engines_list, voice_label
from voiced.engines.kokoro import KokoroEngine

HEAVY = ("fastapi", "starlette", "numpy", "pysilero_vad", "mlx", "mlx_audio", "voiced.engines")


def test_kokoro_and_phonon_catalogues_without_mlx_or_ptts() -> None:
    code = (
        "import json, sys\n"
        "from voiced.engines.catalogue import main\n"
        "main([])\n"
        "mlx = [m for m in sys.modules if m.split('.')[0] in ('mlx', 'mlx_audio', 'ptts')]\n"
        "print(json.dumps(mlx))\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], check=True, capture_output=True, text=True
    ).stdout.splitlines()
    tts = {e["name"]: e for e in json.loads(out[0])["tts"]}
    voices = tts["kokoro"]["voices"]
    assert "af_heart" in voices and "bf_emma" in voices
    assert not any(v.startswith("jf_") for v in voices)
    assert tts["phonon"]["voices"][:2] == ["default", "Zoey"]
    # The catalogue's models, the default first.
    assert tts["phonon"]["models"] == ["gradium/phonon-31m-en", "gradium/phonon-40m-en"]
    assert json.loads(out[1]) == []


def test_labels() -> None:
    result = engines_list()
    tts = {e["name"]: e for e in result["tts"]}
    stt = {e["name"]: e for e in result["stt"]}
    assert tts["kokoro"]["label"] == "Kokoro"
    assert tts["kokoro"]["voice_labels"]["af_heart"] == "Heart (American English, female)"
    assert tts["kokoro"]["voice_labels"]["bm_george"] == "George (British English, male)"
    assert set(tts["kokoro"]["voice_labels"]) == set(tts["kokoro"]["voices"])
    assert tts["soprano"]["voice_labels"] == {"default": "Default"}
    assert tts["phonon"]["voice_labels"]["Easton"] == "Easton (American English, male)"
    assert tts["phonon"]["voice_labels"]["The Snow Queen"] == "The Snow Queen"
    assert tts["phonon"]["voice_languages"]["Maeve"] == "en-GB"
    assert tts["kokoro"]["voice_languages"]["bm_george"] == "en-GB"
    assert set(tts["kokoro"]["voice_languages"]) == set(tts["kokoro"]["voices"])
    assert stt["parakeet"] == {
        "name": "parakeet",
        "label": "Parakeet",
        "models": ["mlx-community/parakeet-tdt-0.6b-v3"],
        "languages": stt["parakeet"]["languages"],
    }
    assert voice_label(Voice("x", "X", "xx-XX", "male")) == "X (xx-XX, male)"


def test_the_watchdog_imports_no_engine_plugins() -> None:
    """``voiced watchdog`` and everything the control socket runs in it import
    neither FastAPI nor numpy nor any engine (ADR 0012, ADR 0013)."""
    code = (
        "import asyncio, json, pathlib, sys\n"
        "import voiced.cli, voiced.control.server, voiced.control.supervisor\n"
        "from voiced.control.ops import NoSupervisor, Operations\n"
        "ops = Operations(NoSupervisor(), pathlib.Path('/nonexistent/config.toml'))\n"
        "listed = asyncio.run(ops.dispatch('engines.list', {}))\n"
        "assert any(e['name'] == 'kokoro' for e in listed['tts'])\n"
        "print(json.dumps(sorted(sys.modules)))\n"
    )
    out = subprocess.run([sys.executable, "-c", code], check=True, capture_output=True, text=True)
    loaded = json.loads(out.stdout)
    heavy = [m for m in loaded if any(m == h or m.startswith(f"{h}.") for h in HEAVY)]
    assert heavy == []


@pytest.mark.model
async def test_kokoro_catalogue_matches_loaded_voices() -> None:
    engine = KokoroEngine(warmup=False)
    await engine.load()
    loaded = {v.id for v in await engine.voices()}
    assert loaded <= {v.id for v in KokoroEngine.catalogue_voices()}
