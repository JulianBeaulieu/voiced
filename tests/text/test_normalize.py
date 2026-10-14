from __future__ import annotations

import pytest

from voiced.text import normalize
from voiced.text.normalize import number_to_words, ordinal_to_words, year_to_words

CASES: list[tuple[str, str]] = [
    # acronyms
    ("The CO2 level is high.", "The C O two level is high."),
    ("HVAC", "H V eh C."),
    ("Plug in the USB and the LEDs.", "Plug in the U S B and the L E D's."),
    ("NASA's rocket is OK.", "NASA's rocket is OK."),
    (
        "Use H2O and MP3 files in 3D at 4K with 2FA.",
        "Use H two O and M P three files in three D at four K with two F eh.",
    ),
    ("The APIs are fast.", "The eh P I's are fast."),
    ("I AM HERE NOW.", "I am here now."),
    ("Compare CPU, GPU, RAM and SSD.", "Compare C P U, G P U, RAM and S S D."),
    ("The PDF needs IT support.", "The P D F needs I T support."),
    ("WARNING: the file is gone.", "WARNING: the file is gone."),
    ("World War II ended.", "World War two ended."),
    # numbers
    ("There are 412 of them.", "There are four hundred twelve of them."),
    ("Pi is 3.14 roughly.", "Pi is three point one four roughly."),
    ("It sold 1,000,000 copies.", "It sold one million copies."),
    ("Agent 007 reporting.", "Agent zero zero seven reporting."),
    ("Account 12345678901.", "Account one two three four five six seven eight nine zero one."),
    ("It is -5 outside.", "It is minus five outside."),
    ("Pages 5-10.", "Pages five to ten."),
    ("Call 555-123-4567.", "Call five five five, one two three, four five six seven."),
    ("version 2.0 is out", "version two point zero is out."),
    ("x86 and mp3 stay.", "x86 and mp3 stay."),
    ("There are 2048 tokens.", "There are two thousand forty eight tokens."),
    ("Use \u00bd cup, then 2\u00bd hours.", "Use one half cup, then two and a half hours."),
    ("Version 3.11.2 is out.", "Version three point eleven point two is out."),
    ("It sold 1,000,000,000 copies.", "It sold one billion copies."),
    ("It is 2.5x faster, 2x more.", "It is two point five x faster, two x more."),
    # ordinals, years, decades
    (
        "The 21st century, 2nd place, 103rd.",
        "The twenty first century, second place, one hundred third.",
    ),
    ("In 1999 we did it.", "In nineteen ninety nine we did it."),
    ("Since 2007.", "Since two thousand seven."),
    ("In 2026.", "In twenty twenty six."),
    ("In the 1990s and the 2020s.", "In the nineteen nineties and the twenty twenties."),
    ("From 2024-2026.", "From twenty twenty four to twenty twenty six."),
    # times
    ("It's 8:14 pm now.", "It's eight fourteen P M now."),
    ("Meet at 8:00.", "Meet at eight o'clock."),
    ("At 14:30 sharp.", "At fourteen thirty sharp."),
    ("It's 10 am and 3 AM.", "It's ten eh M and three eh M."),
    ("At 9:05.", "At nine oh five."),
    ("A 16:9 screen and a 3:1 ratio.", "A sixteen to nine screen and a three to one ratio."),
    ("Ends at 5 pm. Bring snacks.", "Ends at five P M. Bring snacks."),
    ("Ends at 5 p.m. Bring snacks.", "Ends at five P M. Bring snacks."),
    ("He left at 3 p.m. on Monday.", "He left at three P M on Monday."),
    # dates
    ("On 2026-10-14.", "On October fourteenth, twenty twenty six."),
    ("On 10/14/2026.", "On October fourteenth, twenty twenty six."),
    ("On Oct 14, 2026.", "On October fourteenth, twenty twenty six."),
    ("On 14 October 2026.", "On fourteenth of October twenty twenty six."),
    ("In March 2026.", "In March twenty twenty six."),
    # money
    ("It costs $5.50.", "It costs five dollars and fifty cents."),
    ("Just $1.", "Just one dollar."),
    ("Only $0.99!", "Only ninety nine cents!"),
    ("About $1,200.", "About one thousand two hundred dollars."),
    (
        "Worth $5 million or $1.5B or $5k.",
        "Worth five million dollars or one point five billion dollars or five thousand dollars.",
    ),
    ("That is \u00a32.50 or \u20ac3.", "That is two pounds and fifty pence or three euros."),
    ("Pay -$5 or $-5.", "Pay minus five dollars or minus five dollars."),
    # percent, degrees, units
    ("Humidity 45%.", "Humidity forty five percent."),
    (
        "It is -5\u00b0C, or 23 \u00b0F, or 25\u00b0.",
        "It is minus five degrees Celsius, or twenty three degrees Fahrenheit, "
        "or twenty five degrees.",
    ),
    (
        "Drive 5km at 60 km/h for 1 hr.",
        "Drive five kilometers at sixty kilometers per hour for one hour.",
    ),
    (
        "Battery 3000mAh, disk 1.5TB.",
        "Battery three thousand milliamp hours, disk one point five terabytes.",
    ),
    ("Wait 5 min, not 5 minutes.", "Wait five minutes, not five minutes."),
    ("It is 412 ppm.", "It is four hundred twelve parts per million."),
    ("He is 6 ft. tall.", "He is six feet tall."),
    # abbreviations and symbols
    (
        "Fruit, e.g. apples, i.e. food, etc. Next.",
        "Fruit, for example, apples, that is, food, etcetera. Next.",
    ),
    (
        "R&D, #5, 5+3=8, a -> b, ~5, 3\u00d74, snake_case.",
        "R and D, number five, five plus three equals eight, a to b, about five, "
        "three by four, snake case.",
    ),
    ("Mail foo@bar.com today.", "Mail foo at bar dot com today."),
    ("See https://www.example.com/path now.", "See example dot com/path now."),
    # punctuation and whitespace
    ("wait... what?!?! Really!!!", "wait... what? Really!"),
    ("She said \u201chello\u201d \u2014 then left\u2026 ok", 'She said "hello", then left... ok.'),
    ("Hello \U0001f44b world \U0001f680", "Hello world."),
    ("  too   many    spaces  ", "too many spaces."),
    ("Hello", "Hello."),
    ("", ""),
    ("   ", ""),
    # markdown
    ("# Title\n\nSome *bold* **text** with `code`.", "Title. Some bold text with code."),
    ("See [the docs](https://example.com) and ![img](a.png).", "See the docs and img."),
    ("- apples\n- pears\n\n1. First\n2. Second", "apples. pears. one, First. two, Second."),
    ("| a | b |\n|---|---|\n| 1 | 2 |", "a, b. one, two."),
    ("Before\n\n```\ncode here\n```\nAfter", "Before. After."),
    ("> quoted line\n\n---\n\nnext", "quoted line. next."),
    ("soft\nwrapped\nparagraph.", "soft wrapped paragraph."),
    ("Steps:\n1. Wait 5 min\n2. Add 2 eggs", "Steps: one, Wait five minutes. two, Add two eggs."),
    ("<b>bold</b> &amp; done", "bold and done."),
    ("Sub CO\u2082 and x\u00b2.", "Sub C O two and x2."),
]


@pytest.mark.parametrize(("text", "expected"), CASES, ids=[c[0][:40] for c in CASES])
def test_normalize(text: str, expected: str) -> None:
    assert normalize(text) == expected


@pytest.mark.parametrize(("text", "expected"), CASES, ids=[c[0][:40] for c in CASES])
def test_normalize_is_idempotent(text: str, expected: str) -> None:
    assert normalize(expected) == expected
    assert normalize(normalize(text)) == normalize(text)


@pytest.mark.parametrize(
    ("n", "words"),
    [
        (0, "zero"),
        (7, "seven"),
        (13, "thirteen"),
        (20, "twenty"),
        (42, "forty two"),
        (100, "one hundred"),
        (101, "one hundred one"),
        (999, "nine hundred ninety nine"),
        (1000, "one thousand"),
        (1234, "one thousand two hundred thirty four"),
        (1_000_000, "one million"),
        (2_500_010, "two million five hundred thousand ten"),
        (10**9, "one billion"),
        (10**12 + 1, "one trillion one"),
        (-8, "minus eight"),
    ],
)
def test_number_to_words(n: int, words: str) -> None:
    assert number_to_words(n) == words


@pytest.mark.parametrize(
    ("n", "words"),
    [
        (1, "first"),
        (2, "second"),
        (3, "third"),
        (4, "fourth"),
        (5, "fifth"),
        (8, "eighth"),
        (9, "ninth"),
        (12, "twelfth"),
        (20, "twentieth"),
        (21, "twenty first"),
        (100, "one hundredth"),
        (103, "one hundred third"),
    ],
)
def test_ordinal_to_words(n: int, words: str) -> None:
    assert ordinal_to_words(n) == words


@pytest.mark.parametrize(
    ("year", "words"),
    [
        (1999, "nineteen ninety nine"),
        (1905, "nineteen oh five"),
        (1900, "nineteen hundred"),
        (2000, "two thousand"),
        (2007, "two thousand seven"),
        (2010, "twenty ten"),
        (2026, "twenty twenty six"),
        (2100, "twenty one hundred"),
        (800, "eight hundred"),
    ],
)
def test_year_to_words(year: int, words: str) -> None:
    assert year_to_words(year) == words


def test_language_parameter() -> None:
    assert normalize("5 cats", language="en-GB") == "five cats."
    with pytest.raises(ValueError, match="de"):
        normalize("5 Katzen", language="de")


def test_output_has_no_digits_for_plain_prose() -> None:
    text = "On 2026-10-14 at 8:14 pm, 3 people paid $5.50 each for 2.5 kg, a 45% discount."
    assert not any(ch.isdigit() for ch in normalize(text))


def test_long_input_is_handled() -> None:
    text = "Sentence number 1. " * 500
    out = normalize(text)
    assert out.startswith("Sentence number one.")
    assert out.count("Sentence number one.") == 500
