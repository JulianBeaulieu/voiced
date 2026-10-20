"""Text processing that runs before any TTS engine.

:func:`normalize` strips Markdown and, for English voices, turns numbers,
dates and acronyms into speakable words; other languages keep their digits
and symbols as written (ADR 0023). :func:`split_sentences` cuts text into
chunks for streaming synthesis. Both take the language of the voice that will
speak the text, are pure Python with no model and no dependency on the server
layers, so engines and the HTTP and Wyoming layers can all import them.
"""

from voiced.text.language import primary_subtag
from voiced.text.normalize import normalize
from voiced.text.sentences import split_sentences

__all__ = ["normalize", "primary_subtag", "split_sentences"]
