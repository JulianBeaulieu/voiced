"""Engine catalogues are known without loading weights."""

from __future__ import annotations

import subprocess
import sys

import pytest

from voiced.engines.catalogue import EngineCatalogue, catalogue_of
from voiced.engines.kokoro import KokoroEngine
from voiced.testing import FakeSTTEngine


def test_kokoro_catalogue_without_mlx() -> None:
    code = (
        "import sys\n"
        "from voiced.control.ops import engines_list\n"
        "tts = {e['name']: e for e in engines_list()['tts']}\n"
        "assert 'af_heart' in tts['kokoro']['voices'], tts['kokoro']\n"
        "assert 'bf_emma' in tts['kokoro']['voices']\n"
        "assert not any(v.startswith('jf_') for v in tts['kokoro']['voices'])\n"
        "assert not [m for m in sys.modules if m.split('.')[0] in ('mlx', 'mlx_audio')]\n"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


def test_engine_without_catalogue_uses_class_attributes() -> None:
    assert catalogue_of(FakeSTTEngine) == EngineCatalogue(models=["fake/stt"], languages=["en"])


@pytest.mark.model
async def test_kokoro_catalogue_matches_loaded_voices() -> None:
    engine = KokoroEngine(warmup=False)
    await engine.load()
    loaded = {v.id for v in await engine.voices()}
    assert loaded <= set(KokoroEngine.catalogue().voices)
