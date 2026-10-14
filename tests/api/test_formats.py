from __future__ import annotations

import json

from voiced.api import formats
from voiced.engines.base import Segment, Transcript

TWO = Transcript(
    text="hello world. and again.",
    language="en",
    duration_s=3.25,
    segments=(Segment(0.0, 1.5, "hello world."), Segment(1.5, 3.25, "and again.")),
)
FLAT = Transcript(text="no timing here", language="en", duration_s=2.0)
EMPTY = Transcript(text="", language=None, duration_s=1.0)


def test_json_is_openai_shape() -> None:
    body, media = formats.render(TWO, "json")
    assert media == "application/json"
    assert json.loads(body) == {"text": "hello world. and again."}


def test_verbose_json_has_segments_duration_language() -> None:
    body, _ = formats.render(TWO, "verbose_json")
    doc = json.loads(body)
    assert doc["task"] == "transcribe"
    assert doc["language"] == "en"
    assert doc["duration"] == 3.25
    assert doc["text"] == TWO.text
    assert [s["text"] for s in doc["segments"]] == ["hello world.", "and again."]
    assert doc["segments"][1] == {
        "id": 1,
        "seek": 0,
        "start": 1.5,
        "end": 3.25,
        "text": "and again.",
        "tokens": [],
        "temperature": 0.0,
        "avg_logprob": 0.0,
        "compression_ratio": 1.0,
        "no_speech_prob": 0.0,
    }


def test_text_has_trailing_newline_and_empty_stays_empty() -> None:
    assert formats.render(TWO, "text")[0] == "hello world. and again.\n"
    assert formats.render(EMPTY, "text")[0] == ""


def test_srt() -> None:
    body, media = formats.render(TWO, "srt")
    assert media.startswith("text/plain")
    assert body == (
        "1\n00:00:00,000 --> 00:00:01,500\nhello world.\n\n"
        "2\n00:00:01,500 --> 00:00:03,250\nand again.\n"
    )


def test_vtt() -> None:
    body, media = formats.render(TWO, "vtt")
    assert media.startswith("text/vtt")
    assert body == (
        "WEBVTT\n\n00:00:00.000 --> 00:00:01.500\nhello world.\n\n"
        "00:00:01.500 --> 00:00:03.250\nand again.\n"
    )


def test_untimed_transcript_becomes_one_cue_spanning_duration() -> None:
    assert formats.srt(FLAT) == "1\n00:00:00,000 --> 00:00:02,000\nno timing here\n"
    assert formats.vtt(EMPTY) == "WEBVTT\n"
    assert json.loads(formats.render(EMPTY, "verbose_json")[0])["segments"] == []


def test_timestamps_roll_over_hours() -> None:
    t = Transcript(text="late", duration_s=3725.5, segments=(Segment(3600.0, 3725.5, "late"),))
    assert "01:00:00,000 --> 01:02:05,500" in formats.srt(t)
