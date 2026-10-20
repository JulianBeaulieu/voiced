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
sentence-final `。`. Version 1 serves English only (README, Roadmap), but
engines that speak other languages are being added, and the text pipeline
must not be what stops them.

## Decision

The language is the selected voice's, and `normalize` requires it as a
keyword argument, so no caller falls back to English by leaving it out.
`voiced.text.primary_subtag` is the one way to reduce a tag to its primary
subtag (`en-US`, `en_GB` and `EN` all give `en`); `ServiceContext` uses it
too. `split_sentences` takes no language: its rules are the same in every
language.

- **Where the language comes from.** `ServiceContext.check_voice`, which
  both transports already call to resolve the client's voice, returns the
  served `Voice` instead of its id. The route and the Wyoming handler pass
  `voice.language` to `normalize`. `engines/base.py` and
  `SentenceTTSEngine` are unchanged.
- **Normalization.** The rewrites into words live in a table keyed by
  primary subtag, `_SPELLING_PASSES`, with one entry: `en`. Every language
  gets the cleanup that does not depend on it: control characters,
  Markdown, emoji, quotes, `#`, `_`, dashes, the ellipsis, punctuation and
  whitespace. A language without an entry keeps its digits, dates and
  symbols as written: an engine that speaks the language reads "20" better
  than "twenty", and no other language's number words are added here.
  Adding a language means adding its entry, with its tests.
- **Sentence ends.** `voiced.text.sentences` defines the terminators once,
  and `normalize` imports them. `。！？` end a sentence with or without
  whitespace after them, as CJK is written; `.`, `!`, `?`, the ellipsis,
  the Arabic question mark `؟` and the Devanagari danda `।` and double
  danda `॥` end one when whitespace follows. A run that mixes the two
  (`？!`) is one terminator and needs no whitespace. Closing quotes and
  brackets after a terminator, CJK (`」』）》】`) and Arabic (`»`) included,
  stay with the sentence. `normalize` adds no `.` after any terminator,
  and drops a trailing CJK or Arabic comma, semicolon or colon as it drops
  a Latin one.
- **Numbers before a period.** "Am 3. Oktober ist Feiertag" is one
  sentence in German, where an integer with a period is an ordinal, and
  German digits now reach the splitter unspelled. So an integer (thousands
  separators allowed, "1,000.") followed by a period and a capitalised
  word never ends a sentence, in any language; a decimal ("Pi is 3.14.
  Really") still does, as an ordinal never has a decimal point. A list
  marker or section number at the start of a sentence ("1. Erstens", "2.3.
  Title") stays with its item, as before. "It costs 5. Then 6." is now one
  chunk where it was two, but audible English is unchanged: `normalize`
  spells the digits ("five.") before the engine splits. Only raw English
  over the Wyoming stream, which splits before normalizing, gets a longer
  first chunk, the cheaper mistake. German "Seite 12. Dann weiter."
  staying one sentence is the known ambiguity.
- **Abbreviations.** The list is English and applies to every language: in
  another language a word on it ("mar" in Spanish) can only keep two
  sentences together.

## Alternatives considered

- **Normalize inside the engine** (in `SentenceTTSEngine`, which knows the
  voice). `engines/base.py` is frozen and promises engines normalized
  text; third-party engines rely on that.
- **A language for `split_sentences`**, so that an integer before a period
  ends a sentence in English only. It splits "It costs 5. Then" as before,
  but needs the voice's language in the engine and in the Wyoming stream,
  a second voice lookup, and a fallback language for a voice the engine
  does not list, all for a rule that never fires on normalized English.
- **A default of `"en"` for `normalize`'s `language`.** The original bug
  was callers that never passed a language; a default lets the next caller
  repeat it.

## Consequences

- English output is unchanged.
- A non-English voice hears digits, `$`, `%` and `&` as written; whether
  that sounds right depends on the engine's own front end until voiced has
  rules for the language.
- The Wyoming streaming path releases CJK sentences as soon as the next
  one starts, without waiting for whitespace.
- Text in another language without a terminator at the end gets a Latin
  `.` appended, as every block without one does ("小雨" becomes "小雨.").
- Known limits: a long CJK sentence without spaces is still cut hard at the
  length limit (`cut_to_length` knows no CJK clause marks); a terminator
  after a closer starts a new run (`好。」。好` leaves a lone `。`); French
  spaced guillemets (`« bonjour. »`) leave the `»` with the next sentence;
  German `„Hallo.“` never splits, as `“` is not a closer (as before).
