"""Render a :class:`~voiced.engines.base.Transcript` in the OpenAI response formats.

``json`` and ``verbose_json`` follow the shapes the official OpenAI clients
parse (``Transcription`` and ``TranscriptionVerbose``). ``text`` is the bare
transcript, ``srt`` and ``vtt`` are subtitle files with one cue per segment.
Engines without timing information produce one cue spanning the whole input.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from typing import Any, Literal, get_args

from voiced.engines.base import Segment, Transcript

ResponseFormat = Literal["json", "text", "verbose_json", "srt", "vtt"]
RESPONSE_FORMATS: tuple[str, ...] = get_args(ResponseFormat)


def render(transcript: Transcript, fmt: ResponseFormat) -> tuple[str, str]:
    """Return ``(body, media_type)`` for ``fmt``."""
    renderer, media_type = _FORMATS[fmt]
    return renderer(transcript), media_type


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
    """Numbered cues, ``HH:MM:SS,mmm`` timestamps, no header."""
    return _subtitles(transcript, sep=",", header=None, numbered=True)


def vtt(transcript: Transcript) -> str:
    """``WEBVTT`` header, unnumbered cues, ``HH:MM:SS.mmm`` timestamps."""
    return _subtitles(transcript, sep=".", header="WEBVTT\n", numbered=False)


def _subtitles(transcript: Transcript, *, sep: str, header: str | None, numbered: bool) -> str:
    blocks = [] if header is None else [header]
    for i, seg in enumerate(_cues(transcript), start=1):
        number = f"{i}\n" if numbered else ""
        blocks.append(f"{number}{_stamp(seg.start, sep)} --> {_stamp(seg.end, sep)}\n{seg.text}\n")
    return "\n".join(blocks)


def _text(transcript: Transcript) -> str:
    return transcript.text + "\n" if transcript.text else ""


_FORMATS: dict[str, tuple[Callable[[Transcript], str], str]] = {
    "json": (lambda t: json.dumps({"text": t.text}), "application/json"),
    "verbose_json": (lambda t: json.dumps(verbose_json(t)), "application/json"),
    "text": (_text, "text/plain; charset=utf-8"),
    "srt": (srt, "text/plain; charset=utf-8"),  # srt has no registered media type
    "vtt": (vtt, "text/vtt; charset=utf-8"),
}
assert set(_FORMATS) == set(RESPONSE_FORMATS)


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
