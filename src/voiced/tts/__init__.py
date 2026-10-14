"""The ``tts`` service process: HTTP API plus Wyoming listener sharing one engine.

Also home of the text pipeline that runs before any engine: :func:`normalize`
turns numbers, dates, acronyms and Markdown into speakable words, and
:func:`split_sentences` cuts the result into chunks for streaming synthesis.
"""

from voiced.tts.app import build_engine, build_tts_app
from voiced.tts.normalize import normalize
from voiced.tts.sentences import split_sentences

__all__ = ["build_engine", "build_tts_app", "normalize", "split_sentences"]
