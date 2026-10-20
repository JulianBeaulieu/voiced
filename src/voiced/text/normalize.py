"""Text normalization that runs before any TTS engine.

Grapheme-to-phoneme front ends are bad at anything that is not a plain word:
espeak reads ``CO2`` as "co two", Soprano lowercases and strips ``8:14`` into
"eight.fourteen", and both read ``$5`` as "dollar five". This module rewrites
such tokens into words so the engines only ever see what a person would read
aloud. It also strips the Markdown that language models produce.

The function is idempotent: ``normalize(normalize(s)) == normalize(s)``. The
HTTP and Wyoming layers call it before handing text to an engine; engines
only chunk what they receive.

The rewrites into words are per language and only English has them
(:data:`SPELLED_LANGUAGES`). Text in any other language gets the cleanup that
does not depend on the language (Markdown, symbols, control characters,
punctuation and whitespace) and keeps its digits as digits: an engine that
speaks the language reads them better than English words (ADR 0023).

Pure Python, no model, no dependencies.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable

__all__ = [
    "SPELLED_LANGUAGES",
    "normalize",
    "number_to_words",
    "ordinal_to_words",
    "year_to_words",
]

# --------------------------------------------------------------------------
# Numbers
# --------------------------------------------------------------------------

_ONES = (
    "zero one two three four five six seven eight nine ten eleven twelve "
    "thirteen fourteen fifteen sixteen seventeen eighteen nineteen"
).split()
_TENS = "_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()
_SCALES = ((10**12, "trillion"), (10**9, "billion"), (10**6, "million"), (10**3, "thousand"))
_ORDINAL_IRREGULAR = {
    "one": "first",
    "two": "second",
    "three": "third",
    "five": "fifth",
    "eight": "eighth",
    "nine": "ninth",
    "twelve": "twelfth",
}


def number_to_words(n: int) -> str:
    """``1234`` -> ``one thousand two hundred thirty four``. No hyphens, no
    "and": both are read fine by espeak and keeping it plain makes the output
    predictable."""
    if n < 0:
        return "minus " + number_to_words(-n)
    if n < 20:
        return _ONES[n]
    if n < 100:
        tens, ones = divmod(n, 10)
        return _TENS[tens] + ("" if ones == 0 else " " + _ONES[ones])
    if n < 1000:
        hundreds, rest = divmod(n, 100)
        return _ONES[hundreds] + " hundred" + ("" if rest == 0 else " " + number_to_words(rest))
    for value, name in _SCALES:
        if n >= value:
            head, rest = divmod(n, value)
            words = number_to_words(head) + " " + name
            return words if rest == 0 else words + " " + number_to_words(rest)
    raise AssertionError("unreachable")


def ordinal_to_words(n: int) -> str:
    """``14`` -> ``fourteenth``, ``21`` -> ``twenty first``."""
    words = number_to_words(n)
    head, _, last = words.rpartition(" ")
    if last in _ORDINAL_IRREGULAR:
        last = _ORDINAL_IRREGULAR[last]
    elif last.endswith("y"):
        last = last[:-1] + "ieth"
    else:
        last = last + "th"
    return f"{head} {last}".strip()


def year_to_words(year: int) -> str:
    """``1999`` -> ``nineteen ninety nine``, ``2007`` -> ``two thousand seven``."""
    if not 1000 <= year <= 9999:
        return number_to_words(year)
    century, rest = divmod(year, 100)
    if century % 10 == 0 and rest < 10:
        return number_to_words(year)
    if rest == 0:
        return number_to_words(century) + " hundred"
    if rest < 10:
        return number_to_words(century) + " oh " + _ONES[rest]
    return number_to_words(century) + " " + number_to_words(rest)


def _digits_to_words(digits: str) -> str:
    return " ".join(_ONES[int(d)] for d in digits)


def _integer_to_words(token: str) -> str:
    """Integer token (optionally comma grouped) to words. A comma-grouped
    number is always read as a number; a bare digit string with a leading
    zero or ten or more digits (ids, phone numbers) is read digit by digit."""
    digits = token.replace(",", "")
    if "," not in token and ((len(digits) > 1 and digits[0] == "0") or len(digits) >= 10):
        return _digits_to_words(digits)
    return number_to_words(int(digits))


def _decimal_to_words(whole: str, fraction: str | None) -> str:
    words = _integer_to_words(whole)
    if fraction:
        words += " point " + _digits_to_words(fraction)
    return words


def _plural(count_words: str, singular: str, plural: str) -> str:
    return singular if count_words == "one" else plural


# --------------------------------------------------------------------------
# Spelled-out tokens
# --------------------------------------------------------------------------

# Spoken as one letter each. espeak reads an isolated capital "A" as the
# article (a schwa), so it is written phonetically.
_LETTER_WORDS = {"A": "eh"}

# Acronyms that are pronounced as words, not spelled. Anything else of two
# to five capitals (optionally with digits) is spelled: HVAC, CO2, USB.
_SPOKEN_ACRONYMS = frozenset(
    """
    NASA NATO OK OKAY RAM ROM GIF PIN SIM LIDAR RADAR SCUBA LASER UNESCO FIFA
    NAFTA ASAP POTUS SCOTUS JSON YAML NASDAQ SWAT AIDS COVID SARS NASCAR IKEA
    LEGO UNIX LINUX GNOME DOS BIOS GUI WIFI SQL JPEG MPEG NOAA OSHA FEMA
    """.split()
)
# Ordinary English words that show up in capitals (headings, emphasis) and
# must not be spelled.
_UPPERCASE_WORDS = frozenset(
    """
    NO YES NOW NOT THE AND OR IS TO AT IN ON OF BE DO GO SO UP WE YOU ALL
    ANY CAN BUT FOR HAS HAD HIS HER HOW MAY NEW OLD ONE OUR OUT SEE TWO WAS
    WHO WHY WIN DONE STOP HELP OPEN FREE HOME ERROR NOTE TODO END TIP WARN
    """.split()
)
_KEEP_WORDS = _SPOKEN_ACRONYMS | _UPPERCASE_WORDS
_ROMAN = {"II": "two", "III": "three", "VII": "seven", "VIII": "eight"}

_MONTHS = (
    "January February March April May June July August September October November December"
).split()
_MONTH_LOOKUP = {m.lower(): m for m in _MONTHS}
_MONTH_LOOKUP.update({m[:3].lower(): m for m in _MONTHS})
_MONTH_LOOKUP["sept"] = "September"
_MONTH_PATTERN = (
    r"(?:January|February|March|April|May|June|July|August|September|October|"
    r"November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sept|Sep|Oct|Nov|Dec)\.?"
)

# Units spoken after a number: (singular, plural). Case-sensitive on purpose.
_UNITS: dict[str, tuple[str, str]] = {
    "km/h": ("kilometer per hour", "kilometers per hour"),
    "kph": ("kilometer per hour", "kilometers per hour"),
    "mph": ("mile per hour", "miles per hour"),
    "km": ("kilometer", "kilometers"),
    "cm": ("centimeter", "centimeters"),
    "mm": ("millimeter", "millimeters"),
    "mi": ("mile", "miles"),
    "ft": ("foot", "feet"),
    "yd": ("yard", "yards"),
    "kg": ("kilogram", "kilograms"),
    "mg": ("milligram", "milligrams"),
    "lbs": ("pound", "pounds"),
    "lb": ("pound", "pounds"),
    "oz": ("ounce", "ounces"),
    "ml": ("milliliter", "milliliters"),
    "mL": ("milliliter", "milliliters"),
    "L": ("liter", "liters"),
    "ms": ("millisecond", "milliseconds"),
    "sec": ("second", "seconds"),
    "secs": ("second", "seconds"),
    "min": ("minute", "minutes"),
    "mins": ("minute", "minutes"),
    "hr": ("hour", "hours"),
    "hrs": ("hour", "hours"),
    "TB": ("terabyte", "terabytes"),
    "GB": ("gigabyte", "gigabytes"),
    "MB": ("megabyte", "megabytes"),
    "KB": ("kilobyte", "kilobytes"),
    "kB": ("kilobyte", "kilobytes"),
    "Gbps": ("gigabit per second", "gigabits per second"),
    "Mbps": ("megabit per second", "megabits per second"),
    "kbps": ("kilobit per second", "kilobits per second"),
    "GHz": ("gigahertz", "gigahertz"),
    "MHz": ("megahertz", "megahertz"),
    "kHz": ("kilohertz", "kilohertz"),
    "Hz": ("hertz", "hertz"),
    "kWh": ("kilowatt hour", "kilowatt hours"),
    "kW": ("kilowatt", "kilowatts"),
    "MW": ("megawatt", "megawatts"),
    "W": ("watt", "watts"),
    "V": ("volt", "volts"),
    "mAh": ("milliamp hour", "milliamp hours"),
    "mA": ("milliamp", "milliamps"),
    "dB": ("decibel", "decibels"),
    "ppm": ("part per million", "parts per million"),
    "fps": ("frame per second", "frames per second"),
    "rpm": ("R P M", "R P M"),
    "psi": ("P S I", "P S I"),
    "px": ("pixel", "pixels"),
}
_UNIT_PATTERN = "|".join(re.escape(u) for u in sorted(_UNITS, key=len, reverse=True))

_CURRENCIES: dict[str, tuple[str, str, str | None, str | None]] = {
    "$": ("dollar", "dollars", "cent", "cents"),
    "\u20ac": ("euro", "euros", "cent", "cents"),
    "\u00a3": ("pound", "pounds", "penny", "pence"),
    "\u00a5": ("yen", "yen", None, None),
}
_SCALE_WORDS = {"k": "thousand", "m": "million", "b": "billion", "bn": "billion"}

_SUBSCRIPT_DIGITS = str.maketrans(
    "\u2080\u2081\u2082\u2083\u2084\u2085\u2086\u2087\u2088\u2089", "0123456789"
)
_SUPERSCRIPT_DIGITS = str.maketrans(
    "\u2070\u00b9\u00b2\u00b3\u2074\u2075\u2076\u2077\u2078\u2079", "0123456789"
)
_QUOTES = str.maketrans({"\u201c": '"', "\u201d": '"', "\u2018": "'", "\u2019": "'", "\u00b4": "'"})
_FRACTIONS = {
    "\u00bd": "one half",
    "\u2153": "one third",
    "\u2154": "two thirds",
    "\u00bc": "one quarter",
    "\u00be": "three quarters",
}

_NUMBER = r"\d+(?:,\d{3})*(?:\.\d+)?"


# --------------------------------------------------------------------------
# Markdown
# --------------------------------------------------------------------------

_FENCE = re.compile(r"^\s*(```|~~~)")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$")
_BULLET = re.compile(r"^\s*[-*+\u2022]\s+")
_NUMBERED = re.compile(r"^\s*(\d+)[.)]\s+")
_BLOCKQUOTE = re.compile(r"^\s*>\s?")
_HRULE = re.compile(r"^\s*([-*_]\s*){3,}$")
_TABLE_SEPARATOR = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
# The CJK full stop, exclamation and question marks, the Arabic question mark
# and the Devanagari danda end a sentence too.
_TERMINAL = ".!?:;,\u2026\u3002\uff01\uff1f\u061f\u0964"


def _strip_markdown(text: str) -> str:
    """Line-oriented pass: drop code blocks, turn headings, list items and
    table rows into sentences, join soft-wrapped lines."""
    blocks: list[str] = []
    paragraph: list[str] = []
    in_fence = False

    def flush() -> None:
        if paragraph:
            blocks.append(_end_block(" ".join(paragraph)))
            paragraph.clear()

    for raw in text.splitlines():
        if _FENCE.match(raw):
            in_fence = not in_fence
            flush()
            continue
        if in_fence:
            continue
        line = raw.strip()
        if not line or _HRULE.match(line) or _TABLE_SEPARATOR.match(line):
            flush()
            continue
        heading = _HEADING.match(line)
        if heading:
            flush()
            blocks.append(_end_block(heading.group(1)))
            continue
        if _TABLE_ROW.match(line):
            flush()
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            blocks.append(_end_block(", ".join(c for c in cells if c)))
            continue
        line = _BLOCKQUOTE.sub("", line)
        if _BULLET.match(line):
            flush()
            blocks.append(_end_block(_BULLET.sub("", line)))
            continue
        numbered = _NUMBERED.match(line)
        if numbered:
            flush()
            blocks.append(_end_block(f"{numbered.group(1)}, {line[numbered.end() :]}"))
            continue
        paragraph.append(line)
    flush()
    return " ".join(b for b in blocks if b)


def _end_block(text: str) -> str:
    """Add a sentence-final period unless one is already there (possibly behind
    a closing quote or bracket)."""
    text = text.strip()
    last = text.rstrip("\"')]")[-1:]
    if last and last not in _TERMINAL:
        text += "."
    return text


_INLINE_MARKDOWN: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"!\[([^\]]*)\]\([^)]*\)"), r"\1"),  # images -> alt text
    (re.compile(r"\[([^\]]+)\]\([^)]*\)"), r"\1"),  # links -> text
    (re.compile(r"<(https?://[^>]+)>"), r"\1"),
    (re.compile(r"<[^<>\s][^<>]*>"), ""),  # inline html tags
    (re.compile(r"`([^`]*)`"), r"\1"),
    (re.compile(r"\*\*(.+?)\*\*"), r"\1"),
    (re.compile(r"__(.+?)__"), r"\1"),
    (re.compile(r"~~(.+?)~~"), r"\1"),
    (re.compile(r"(?<!\w)\*(\S(?:[^*]*?\S)?)\*(?!\w)"), r"\1"),
    (re.compile(r"(?<!\w)_(\S(?:[^_]*?\S)?)_(?!\w)"), r"\1"),
    (re.compile(r"[*`]+"), ""),
    (re.compile(r"&amp;"), "&"),
    (re.compile(r"&nbsp;"), " "),
    (re.compile(r"&lt;"), "<"),
    (re.compile(r"&gt;"), ">"),
    (re.compile(r"&quot;"), '"'),
    (re.compile(r"https?://(?:www\.)?"), ""),
    (re.compile(r"\bwww\."), ""),
)


# --------------------------------------------------------------------------
# Token rewrites
# --------------------------------------------------------------------------


def _currency(m: re.Match[str]) -> str:
    symbol, negative, whole, fraction = m.group(1), m.group(2), m.group(3), m.group(4)
    scale = m.group(5) or m.group(6)
    return ("minus " if negative else "") + _amount(symbol, whole, fraction, scale)


def _amount(symbol: str, whole: str, fraction: str | None, scale: str | None) -> str:
    singular, plural, minor_singular, minor_plural = _CURRENCIES[symbol]
    if scale:
        amount = _decimal_to_words(whole, fraction)
        scale_word = _SCALE_WORDS.get(scale.lower(), scale.lower())
        return f"{amount} {scale_word} {plural}"
    major = _integer_to_words(whole)
    cents = int((fraction or "0")[:2].ljust(2, "0"))
    if cents and minor_singular and minor_plural:
        minor = number_to_words(cents)
        minor_unit = _plural(minor, minor_singular, minor_plural)
        if whole.strip("0,") == "":
            return f"{minor} {minor_unit}"
        return f"{major} {_plural(major, singular, plural)} and {minor} {minor_unit}"
    if fraction and ((cents == 0 and len(fraction) > 2) or not minor_singular):
        return f"{_decimal_to_words(whole, fraction)} {plural}"
    return f"{major} {_plural(major, singular, plural)}"


def _unit(m: re.Match[str]) -> str:
    amount = _decimal_to_words(m.group(1), m.group(2))
    singular, plural = _UNITS[m.group(3)]
    return f"{amount} {_plural(amount, singular, plural)}"


def _degrees(m: re.Match[str]) -> str:
    amount = _decimal_to_words(m.group(1), m.group(2))
    scale = {"C": " Celsius", "F": " Fahrenheit"}.get(m.group(3) or "", "")
    return f"{amount} {_plural(amount, 'degree', 'degrees')}{scale}"


def _percent(m: re.Match[str]) -> str:
    return f"{_decimal_to_words(m.group(1), m.group(2))} percent"


def _time(m: re.Match[str]) -> str:
    hour, minute, meridiem = int(m.group(1)), m.group(2), m.group(3)
    if hour > 24 or (minute is not None and int(minute) > 59):
        return m.group(0)
    if meridiem and hour > 12:
        return m.group(0)
    words = number_to_words(hour)
    minutes = int(minute) if minute is not None else 0
    if minutes == 0:
        if not meridiem:
            words += " o'clock"
    elif minutes < 10:
        words += " oh " + _ONES[minutes]
    else:
        words += " " + number_to_words(minutes)
    if meridiem:
        words += " eh M" if meridiem[0].lower() == "a" else " P M"
    return words


def _iso_date(m: re.Match[str]) -> str:
    year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return m.group(0)
    return f"{_MONTHS[month - 1]} {ordinal_to_words(day)}, {year_to_words(year)}"


def _slash_date(m: re.Match[str]) -> str:
    first, second, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
    month, day = (first, second) if first <= 12 else (second, first)
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return m.group(0)
    return f"{_MONTHS[month - 1]} {ordinal_to_words(day)}, {year_to_words(year)}"


def _month_day(m: re.Match[str]) -> str:
    month = _MONTH_LOOKUP[m.group(1).rstrip(".").lower()]
    day = int(m.group(2))
    if not 1 <= day <= 31:
        return m.group(0)
    out = f"{month} {ordinal_to_words(day)}"
    if m.group(3):
        out += f", {year_to_words(int(m.group(3)))}"
    return out


def _day_month(m: re.Match[str]) -> str:
    day = int(m.group(1))
    month = _MONTH_LOOKUP[m.group(2).rstrip(".").lower()]
    if not 1 <= day <= 31:
        return m.group(0)
    out = f"{ordinal_to_words(day)} of {month}"
    if m.group(3):
        out += f" {year_to_words(int(m.group(3)))}"
    return out


def _month_year(m: re.Match[str]) -> str:
    return f"{_MONTH_LOOKUP[m.group(1).rstrip('.').lower()]} {year_to_words(int(m.group(2)))}"


def _ordinal(m: re.Match[str]) -> str:
    return ordinal_to_words(int(m.group(1)))


def _year_in_context(m: re.Match[str]) -> str:
    return m.group(1) + year_to_words(int(m.group(2)))


def _year_range(m: re.Match[str]) -> str:
    return f"{year_to_words(int(m.group(1)))} to {year_to_words(int(m.group(2)))}"


def _decade(m: re.Match[str]) -> str:
    words = year_to_words(int(m.group(1)))
    if words.endswith("y"):
        return words[:-1] + "ies"
    return words + "s"


def _phone(m: re.Match[str]) -> str:
    return ", ".join(_digits_to_words(g) for g in m.groups())


def _number_range(m: re.Match[str]) -> str:
    return f"{m.group(1)} to {m.group(2)}"


def _version(m: re.Match[str]) -> str:
    """Dotted versions: ``3.11.2`` -> ``three point eleven point two``."""
    return " point ".join(number_to_words(int(part)) for part in m.group(0).split("."))


def _number(m: re.Match[str]) -> str:
    return _decimal_to_words(m.group(1), m.group(2))


_SHOUTING = re.compile(r"\b[A-Z]{2,}(?:[\s,]+[A-Z]{2,}\b){2,}")


def _unshout(m: re.Match[str]) -> str:
    """Three or more capitalised words in a row are shouting ("I AM HERE NOW"),
    unless every token could be an acronym ("CPU, GPU, RAM and SSD" is a list
    of them). A run is shouting when it contains a word longer than five
    letters or a known English word."""
    words = re.findall(r"[A-Z]+", m.group(0))
    if any(len(w) > 5 or w in _UPPERCASE_WORDS for w in words):
        return m.group(0).lower()
    return m.group(0)


def _acronym(m: re.Match[str]) -> str:
    token, plural = m.group(1), m.group(2) or ""
    if token in _KEEP_WORDS or token.isdigit():
        return m.group(0)
    if token in _ROMAN:
        return _ROMAN[token]
    if token.isalpha() and len(token) > 5:
        return m.group(0)
    parts = [_LETTER_WORDS.get(c, c) if c.isalpha() else _ONES[int(c)] for c in token]
    if plural:
        parts[-1] += "'s"
    return " ".join(parts)


def _fraction(m: re.Match[str]) -> str:
    whole, frac = m.group(1), _FRACTIONS[m.group(2)]
    if not whole:
        return frac
    article = "a " if frac.startswith("one ") else ""
    return f"{whole} and {article}{frac.removeprefix('one ')}"


_Pass = tuple[re.Pattern[str], "str | Callable[[re.Match[str]], str]"]

# Abbreviations that are read as phrases.
_ABBREVIATION_PASSES: tuple[_Pass, ...] = (
    (re.compile(r"\be\.g\.,?\s*", re.I), "for example, "),
    (re.compile(r"\bi\.e\.,?\s*", re.I), "that is, "),
    (re.compile(r"\betc\.(?=\s*[a-z])"), "etcetera"),
    (re.compile(r"\betc\.?(?=\s*[.!?,;:]|\s*$)"), "etcetera"),
    (re.compile(r"\bvs\.?\s", re.I), "versus "),
)

# Web addresses and mail: the scheme is already gone; make the dot audible.
_WEB_PASSES: tuple[_Pass, ...] = (
    (re.compile(r"(\w)\.(com|org|net|io|dev|ai|app|edu|gov|co\.uk|de)\b"), r"\1 dot \2"),
    (re.compile(r"(\w)@(\w)"), r"\1 at \2"),
)

# Phone numbers and dates, before anything else eats their digits.
_DATE_PASSES: tuple[_Pass, ...] = (
    (re.compile(r"\b(\d{3})[-. ](\d{3})[-. ](\d{4})\b"), _phone),
    (re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b"), _iso_date),
    (re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b"), _slash_date),
    (
        re.compile(rf"\b({_MONTH_PATTERN})\s+(\d{{1,2}})(?:st|nd|rd|th)?\b(?:,?\s+(\d{{4}}))?"),
        _month_day,
    ),
    (
        re.compile(
            rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?({_MONTH_PATTERN})(?:,?\s+(\d{{4}}))?\b"
        ),
        _day_month,
    ),
    (re.compile(rf"\b({_MONTH_PATTERN})\s+(1[5-9]\d\d|20\d\d)\b"), _month_year),
)

# Times: 8:14, 8:14 pm, 8 p.m., 14:30. Then ratios and scores: 16:9, 3:1.
_TIME_PASSES: tuple[_Pass, ...] = (
    (
        re.compile(
            r"(?<![\d:.])(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\b(?:\.(?=\s*(?-i:[a-z])))?", re.I
        ),
        _time,
    ),
    (re.compile(r"(?<![\d:.])(\d{1,2}):(\d{2})(?![\d:])()"), _time),
    (re.compile(r"(?<![\d:.])(\d+):(\d+)(?![\d:])"), r"\1 to \2"),
)

# Money, percentages, temperatures, units. A leading minus comes first,
# before the units hide the digit behind a word.
_QUANTITY_PASSES: tuple[_Pass, ...] = (
    (re.compile(r"(?<![\w.$\u20ac\u00a3\u00a5])-(?=[\d$\u20ac\u00a3\u00a5])"), "minus "),
    (
        re.compile(
            r"([$\u20ac\u00a3\u00a5])\s?(-?)(\d+(?:,\d{3})*)(?:\.(\d+))?"
            r"(?:\s*(thousand|million|billion|trillion)\b|(k|K|m|M|bn|B)(?![A-Za-z]))?"
        ),
        _currency,
    ),
    (re.compile(r"(?<![\w.])(\d+(?:,\d{3})*)(?:\.(\d+))?\s?%"), _percent),
    (
        re.compile(r"(?<![\w.])(\d+(?:,\d{3})*)(?:\.(\d+))?\s?\u00b0\s?([CF])?(?![A-Za-z])"),
        _degrees,
    ),
    (
        re.compile(
            rf"(?<![\w.])(\d+(?:,\d{{3}})*)(?:\.(\d+))?\s?({_UNIT_PATTERN})"
            r"(?:\.(?=\s+[a-z]))?(?![A-Za-z])"
        ),
        _unit,
    ),
    (re.compile(r"(\d+)?([\u00bd\u2153\u2154\u00bc\u00be])"), _fraction),
)

# Ordinals, years, decades, ranges, versions, multipliers, then plain
# numbers. Alphanumerics like x86 or mp3 are left to the engine.
_NUMBER_PASSES: tuple[_Pass, ...] = (
    (re.compile(r"\b(\d+)(?:st|nd|rd|th)\b"), _ordinal),
    (re.compile(r"\b(1[5-9]\d0|20\d0)s\b"), _decade),
    (re.compile(r"\b(1[5-9]\d\d|20\d\d)\s?[-\u2013]\s?(1[5-9]\d\d|20\d\d)\b"), _year_range),
    (
        re.compile(
            r"(\b(?:in|since|from|until|till|by|around|before|after|during|late|early|"
            r"mid|circa|between|year|of)\s+)(1[5-9]\d\d|20\d\d)\b(?![\w.,:]\d)",
            re.I,
        ),
        _year_in_context,
    ),
    (re.compile(rf"(?<![\w.])({_NUMBER})[-\u2013]({_NUMBER})(?!\w|\.\d)"), _number_range),
    (re.compile(r"(?<![\w.])\d+(?:\.\d+){2,}(?!\w|\.\d)"), _version),
    (re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)x\b"), r"\1 x"),
    (re.compile(r"#\s?(?=\d)"), "number "),
    (re.compile(r"(?<![\w.])(\d+(?:,\d{3})*)(?:\.(\d+))?(?!\w|\.\d)"), _number),
)

# Acronyms and symbols.
_SYMBOL_PASSES: tuple[_Pass, ...] = (
    (_SHOUTING, _unshout),
    (re.compile(r"(?<![\w'])((?=[A-Z0-9]*[A-Z])[A-Z0-9]{2,6})('?s)?(?![\w'])"), _acronym),
    (re.compile(r"\s*&\s*"), " and "),
    (re.compile(r"(\w)\s*\+\s*(\w)"), r"\1 plus \2"),
    (re.compile(r"\s*=\s*"), " equals "),
    (re.compile(r"\s*(?:->|\u2192)\s*"), " to "),
    (re.compile(r"\u2248\s*"), "about "),
    (re.compile(r"~(?=\w)"), "about "),
    (re.compile(r"(\w)\s*\u00d7\s*(\w)"), r"\1 by \2"),
)

# Rewrites into words, per primary language subtag. A language without an
# entry keeps its numbers, dates and symbols as written.
_SPELLING_PASSES: dict[str, tuple[_Pass, ...]] = {
    "en": (
        _ABBREVIATION_PASSES
        + _WEB_PASSES
        + _DATE_PASSES
        + _TIME_PASSES
        + _QUANTITY_PASSES
        + _NUMBER_PASSES
        + _SYMBOL_PASSES
    ),
}

SPELLED_LANGUAGES = frozenset(_SPELLING_PASSES)
"""Primary subtags whose numbers, dates, units and acronyms become words."""

# Punctuation an engine reads badly in any language, after the spelling.
_PUNCTUATION_PASSES: tuple[_Pass, ...] = (
    (re.compile(r"#"), ""),
    (re.compile(r"_"), " "),
    (re.compile(r"\s*\u2014\s*|\s+[-\u2013]\s+"), ", "),
    (re.compile(r"\u2026"), "..."),
)

_KEEP_SYMBOLS = frozenset("\u00b0")  # the degree sign is a symbol too


def _strip_symbols(text: str) -> str:
    """Drop emoji and other symbols the engines would read as names."""
    out: list[str] = []
    for ch in text:
        category = unicodedata.category(ch)
        if ch == "\ufe0f" or (category in {"So", "Cf", "Co", "Cs"} and ch not in _KEEP_SYMBOLS):
            continue
        out.append(ch)
    return "".join(out)


_PUNCT_CLEANUP: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\s+([,.!?;:])"), r"\1"),
    (re.compile(r"([!?])[!?]+"), r"\1"),
    (re.compile(r"\.{4,}"), "..."),
    (re.compile(r"(?<!\.)\.\.(?!\.)"), "."),
    (re.compile(r",(\s*,)+"), ","),
    (re.compile(r"([.!?])\s*,"), r"\1"),
    (re.compile(r",\s*([.!?])"), r"\1"),
    (re.compile(r"^[\s,.;:]+"), ""),
    (re.compile(r"[\s,;:]+$"), ""),
    (re.compile(r"\s+"), " "),
)


# C0 control characters except tab and newline; a NUL byte stops espeak.
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def normalize(text: str, *, language: str = "en") -> str:
    """Rewrite ``text`` so a TTS engine reads it the way a person would.

    Numbers, times, dates, money, percentages, temperatures and units become
    words; acronyms such as ``CO2`` and ``HVAC`` are spelled; Markdown is
    stripped; smart punctuation and whitespace are tidied. See the module
    docstring for the guarantees.

    ``language`` is the BCP-47 tag of the voice that will speak the text.
    Only the rewrites into words depend on it: they apply to the languages
    in :data:`SPELLED_LANGUAGES` (``en`` and any ``en-*``), and any other
    language keeps its digits and symbols as written.
    """
    spelling = _SPELLING_PASSES.get(re.split(r"[-_]", language)[0].lower(), ())
    text = _CONTROL_CHARS.sub("", text)
    text = unicodedata.normalize("NFC", text)
    text = text.translate(_SUBSCRIPT_DIGITS).translate(_SUPERSCRIPT_DIGITS).translate(_QUOTES)
    text = _strip_markdown(text)
    for pattern, literal in _INLINE_MARKDOWN:
        text = pattern.sub(literal, text)
    text = _strip_symbols(text)
    for pattern, replacement in spelling + _PUNCTUATION_PASSES:
        text = pattern.sub(replacement, text)
    for pattern, literal in _PUNCT_CLEANUP:
        text = pattern.sub(literal, text)
    return _end_block(text)
