"""Text processing that runs before any TTS engine.

:func:`normalize` turns numbers, dates, acronyms and Markdown into speakable
words; :func:`split_sentences` cuts text into chunks for streaming synthesis.
Both are pure Python with no model and no dependency on the server layers,
so engines and the HTTP and Wyoming layers can all import them.
"""

from voiced.text.normalize import normalize
from voiced.text.sentences import split_sentences

__all__ = ["normalize", "split_sentences"]
