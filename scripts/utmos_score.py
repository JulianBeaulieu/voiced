#!/usr/bin/env python3
"""Predict the naturalness (MOS, 1 to 5) of WAV files with UTMOS22 (ADR 0023).

``scripts/bench_engine.py tts`` runs this in a throwaway environment that
uv builds from its ``SCORER_PINS``, so torch never enters voiced's lock::

    uv run --isolated --no-project --python 3.12 \\
        --with torch==2.14.1 --with torchaudio==2.11.0 \\
        python scripts/utmos_score.py a.wav b.wav

It prints one JSON list of scores, in argument order. The model is
tarepan/SpeechMOS's port of the UTMOS22 strong learner (MIT), fetched by
``torch.hub`` at a fixed commit and cached under ``~/.cache/torch/hub``.
"""

from __future__ import annotations

import json
import sys
import wave

import torch

HUB_REPO = "tarepan/SpeechMOS:ed25eacbfa42b99156c36ebec67a733b5dbb9b79"  # tag v1.2.0
HUB_MODEL = "utmos22_strong"


def read_wav(path: str) -> tuple[torch.Tensor, int]:
    """Mono 16-bit PCM as a (1, samples) float tensor, without torchaudio's
    file backends."""
    with wave.open(path) as wav:
        if wav.getnchannels() != 1 or wav.getsampwidth() != 2:
            raise SystemExit(f"{path} must be mono 16-bit PCM")
        rate = wav.getframerate()
        pcm = bytearray(wav.readframes(wav.getnframes()))
    return (torch.frombuffer(pcm, dtype=torch.int16).float() / 32768.0).unsqueeze(0), rate


def main(paths: list[str]) -> int:
    model = torch.hub.load(HUB_REPO, HUB_MODEL, trust_repo=True, verbose=False)
    scores = []
    with torch.no_grad():
        for path in paths:
            wave_, rate = read_wav(path)
            scores.append(round(float(model(wave_, rate)), 3))
    print(json.dumps(scores))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
