#!/usr/bin/env python3
"""Predict the naturalness (MOS, 1 to 5) of WAV files with UTMOS22 (ADR 0024).

``scripts/bench_engine.py tts`` runs this in a throwaway environment that
uv builds from its ``SCORER_PINS``, so torch never enters voiced's lock::

    uv run --isolated --no-project --python 3.12 \\
        --with torch==2.14.1 --with torchaudio==2.11.0 \\
        python scripts/utmos_score.py a.wav b.wav

It prints one JSON list of scores, in argument order. The model is
tarepan/SpeechMOS's port of the UTMOS22 strong learner (MIT): its code
through ``torch.hub`` at a fixed commit, its weights from the release that
commit names, checked against :data:`WEIGHTS_SHA256`, both cached under
``~/.cache/torch/hub``.
"""

from __future__ import annotations

import hashlib
import json
import sys
import wave
from pathlib import Path

import torch

HUB_REPO = "tarepan/SpeechMOS:ed25eacbfa42b99156c36ebec67a733b5dbb9b79"  # tag v1.2.0
HUB_MODEL = "utmos22_strong"
WEIGHTS_URL = (
    "https://github.com/tarepan/SpeechMOS/releases/download/v1.0.0/utmos22_strong_step7459_v1.pt"
)
WEIGHTS_SHA256 = "38aa51ab79e2a4e09a1449758a4b37e9cbb2e8235a49662a732d33a9ba1e9bff"


def read_wav(path: str) -> tuple[torch.Tensor, int]:
    """Mono 16-bit PCM as a (1, samples) float tensor, without torchaudio's
    file backends."""
    with wave.open(path) as wav:
        if wav.getnchannels() != 1 or wav.getsampwidth() != 2:
            raise SystemExit(f"{path} must be mono 16-bit PCM")
        rate = wav.getframerate()
        pcm = bytearray(wav.readframes(wav.getnframes()))
    return (torch.frombuffer(pcm, dtype=torch.int16).float() / 32768.0).unsqueeze(0), rate


def weights() -> Path:
    """The checkpoint, downloaded once and refused if its bytes changed."""
    path = Path(torch.hub.get_dir()) / "checkpoints" / WEIGHTS_URL.rsplit("/", 1)[-1]
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        torch.hub.download_url_to_file(WEIGHTS_URL, str(path), progress=False)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != WEIGHTS_SHA256:
        raise SystemExit(f"{path} has sha256 {digest}, not {WEIGHTS_SHA256}")
    return path


def main(paths: list[str]) -> int:
    model = torch.hub.load(HUB_REPO, HUB_MODEL, trust_repo=True, verbose=False, pretrained=False)
    model.load_state_dict(torch.load(weights(), map_location="cpu"))
    model.eval()
    scores = []
    with torch.no_grad():
        for path in paths:
            wave_, rate = read_wav(path)
            scores.append(round(float(model(wave_, rate)), 3))
    print(json.dumps(scores))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
