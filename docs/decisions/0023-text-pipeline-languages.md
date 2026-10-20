# 0023: Text is normalized for the voice's language

Date: 2026-10-20. Status: accepted.

## Context

`voiced.text.normalize` rewrites numbers, dates, units, symbols and
acronyms into English words, and refused any language but English. The
HTTP route and the Wyoming handler called it without a language, so every
voice got English: "Ich habe 20 Euro" became "Ich habe twenty Euro" and
"3,5" became "three,five". `split_sentences` only knew `.`, `!`, `?` and
the ellipsis, followed by whitespace, so a Chinese or Arabic paragraph was
synthesized as one sentence, and `normalize` appended a `.` after a
sentence-final `。`. English stays the only language served (DECISIONS.md),
but engines that speak others are being added, and the text pipeline must
not be what stops them.

## Decision

The language is the selected voice's. `ServiceContext.check_voice`, which
both transports already call to resolve and check the client's voice, now
returns the served `Voice` instead of its id, and the route and the
Wyoming handler call `normalize(text, language=voice.language)`. The
voice is always resolved by then (an explicit voice, else
`tts.default_voice`), so no fallback language is needed.

`normalize` accepts any BCP-47 tag. The rewrites into words live in a
table keyed by primary subtag, `_SPELLING_PASSES`, with one entry: `en`.
Every language gets the cleanup that does not depend on it: control
characters, Markdown, emoji, quotes, `#`, `_`, dashes, the ellipsis,
punctuation and whitespace. A language without an entry keeps its digits,
dates and symbols as written: an engine that speaks the language reads
"20" better than "twenty", and no other language's number words are added
here. Adding a language means adding its entry, with its tests.

`split_sentences` takes no language: its rules are the same for all. The
CJK full stop, exclamation and question marks (`。！？`) end a sentence with
or without whitespace after them, as CJK is written; the Arabic question
mark (`؟`) and the Devanagari danda (`।`) end one when whitespace follows,
like `.`. None of them is ambiguous in any language. `normalize` adds no `.`
after any of them. Quote and whitespace handling are unchanged.

## Alternatives considered

- **Normalize inside the engine** (in `SentenceTTSEngine`, which knows the
  voice). `engines/base.py` is frozen and promises engines normalized
  text; third-party engines rely on that.
- **A `language` parameter on `split_sentences`.** Nothing in it depends
  on the language yet. The abbreviation list is English, and in another
  language a word on it ("mar" in Spanish) can only keep two sentences
  together, the cheaper mistake for a splitter that is conservative on
  purpose. The parameter can come with the first rule that needs it.
- **A separate lookup of the voice's language** next to `check_voice`.
  That is a second pass over the same voices for what the first already
  found.

## Consequences

- English output is unchanged.
- A non-English voice hears digits, `$`, `%` and `&` as written; whether
  that sounds right depends on the engine's own front end until voiced
  has rules for the language.
- The Wyoming streaming path releases CJK sentences as soon as the next
  one starts, without waiting for whitespace.
- A long CJK sentence without spaces is still cut hard at the length
  limit (`cut_to_length` knows no CJK clause marks).
