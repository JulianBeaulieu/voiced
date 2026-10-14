"""Render a :class:`~voiced.engines.base.Transcript` in the OpenAI response formats.

``json`` and ``verbose_json`` follow the shapes the official OpenAI clients
parse (``Transcription`` and ``TranscriptionVerbose``). ``text`` is the bare
transcript, ``srt`` and ``vtt`` are subtitle files with one cue per segment.
Engines without timing information produce one cue spanning the whole input.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any, Literal, get_args

from voiced.engines.base import Segment, Transcript

ResponseFormat = Literal["json", "text", "verbose_json", "srt", "vtt"]
RESPONSE_FORMATS: tuple[str, ...] = get_args(ResponseFormat)

MEDIA_TYPES: dict[str, str] = {
    "json": "application/json",
    "verbose_json": "application/json",
    "text": "text/plain; charset=utf-8",
    "srt": "text/plain; charset=utf-8",  # srt has no registered media type
    "vtt": "text/vtt; charset=utf-8",
}


def render(transcript: Transcript, fmt: ResponseFormat) -> tuple[str, str]:
    """Return ``(body, media_type)`` for ``fmt``."""
    if fmt == "json":
        body = json.dumps({"text": transcript.text})
    elif fmt == "verbose_json":
        body = json.dumps(verbose_json(transcript))
    elif fmt == "text":
        body = transcript.text + "\n" if transcript.text else ""
    elif fmt == "srt":
        body = srt(transcript)
    elif fmt == "vtt":
        body = vtt(transcript)
    else:  # pragma: no cover - callers validate first
        raise ValueError(f"unknown response_format {fmt!r}")
    return body, MEDIA_TYPES[fmt]


def verbose_json(transcript: Transcript) -> dict[str, Any]:
    """OpenAI ``verbose_json``: ``task``, ``language``, ``duration``, ``text``, ``segments``.

    The per-segment decoder statistics (``avg_logprob`` and friends) exist in
    the OpenAI schema because Whisper exposes them. Our engines do not, so they
    are neutral constants; clients only need the fields to be present.
    """
    segments = [
        {
            "id": i,
            "seek": 0,
            "start": round(seg.start, 3),
            "end": round(seg.end, 3),
            "text": seg.text,
            "tokens": [],
            "temperature": 0.0,
            "avg_logprob": 0.0,
            "compression_ratio": 1.0,
            "no_speech_prob": 0.0,
        }
        for i, seg in enumerate(_cues(transcript))
    ]
    return {
        "task": "transcribe",
        "language": transcript.language or "",
        "duration": round(transcript.duration_s, 3),
        "text": transcript.text,
        "segments": segments,
    }


def srt(transcript: Transcript) -> str:
    out = []
    for i, seg in enumerate(_cues(transcript), start=1):
        out.append(f"{i}\n{_stamp(seg.start, ',')} --> {_stamp(seg.end, ',')}\n{seg.text}\n")
    return "\n".join(out)


def vtt(transcript: Transcript) -> str:
    out = ["WEBVTT\n"]
    for seg in _cues(transcript):
        out.append(f"{_stamp(seg.start, '.')} --> {_stamp(seg.end, '.')}\n{seg.text}\n")
    return "\n".join(out)


def _cues(transcript: Transcript) -> Sequence[Segment]:
    if transcript.segments:
        return transcript.segments
    if transcript.text:
        return (Segment(0.0, max(transcript.duration_s, 0.0), transcript.text),)
    return ()


def _stamp(seconds: float, ms_sep: str) -> str:
    """``HH:MM:SS,mmm`` (srt) or ``HH:MM:SS.mmm`` (vtt)."""
    total_ms = max(round(seconds * 1000), 0)
    hours, rem = divmod(total_ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, ms = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}{ms_sep}{ms:03d}"
