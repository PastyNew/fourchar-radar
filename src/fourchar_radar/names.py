"""Username syntax rules, in one place.

Discord's new (2023+) username system is global and unique. The canonical set
of characters is ``a-z``, ``0-9``, ``_`` and ``.``, with a length of 2..32.
Every source and the checker agree on these rules via this module.
"""

from __future__ import annotations

import re

#: The 37 characters a "clean" short username is built from (no dots).
ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789_"

#: Full accepted set, including dots.
FULL_CHARSET = ALPHABET + "."

VALID_RE = re.compile(r"^[a-z0-9_.]{2,32}$")

VOWELS = frozenset("aeiouy")


def is_valid(name: str) -> bool:
    """True if *name* is syntactically acceptable to Discord."""
    return bool(VALID_RE.match(name))


def normalize(raw: str) -> str:
    """Best-effort cleanup of user-supplied input.

    Trims whitespace, lowercases, drops a leading ``@`` and any surrounding
    Discord mention syntax. Returns ``""`` when nothing usable is left.
    """
    value = raw.strip().strip("<>").lower()
    if value.startswith("@"):
        value = value[1:]
    return value if is_valid(value) else ""


def char_classes(name: str) -> set[str]:
    """Which character classes *name* uses: ``letters``/``digits``/``sep``."""
    classes: set[str] = set()
    for ch in name:
        if ch.isalpha():
            classes.add("letters")
        elif ch.isdigit():
            classes.add("digits")
        else:
            classes.add("sep")
    return classes


def vowel_ratio(name: str) -> float:
    """Share of alphabetic characters that are vowels; 1.0 if no letters."""
    letters = [c for c in name if c.isalpha()]
    if not letters:
        return 1.0
    return sum(1 for c in letters if c in VOWELS) / len(letters)


def repeated_ratio(name: str) -> float:
    """Share of characters that repeat an earlier character; 1.0 = all same."""
    if not name:
        return 0.0
    seen: set[str] = set()
    repeats = 0
    for ch in name:
        if ch in seen:
            repeats += 1
        seen.add(ch)
    return repeats / len(name)
