"""Shared wordlist access.

Lives at the package root because two very different layers need it: the
*scanner* uses it as a candidate source, and the *pricer* uses it to decide
whether a free name is a real English word (the single strongest predictor of
what a short username is worth).

The lists ship as package data, so this works identically from a source
checkout and from an installed wheel.
"""

from __future__ import annotations

from functools import lru_cache
from importlib.resources import files

_PACKAGE = "fourchar_radar.data"
_DEFAULT_FILE = "words4.txt"


@lru_cache(maxsize=1)
def _load(filename: str) -> tuple[str, ...]:
    text = files(_PACKAGE).joinpath(filename).read_text(encoding="utf-8")
    words = (line.strip().lower() for line in text.splitlines())
    return tuple(dict.fromkeys(w for w in words if w and not w.startswith("#")))


def load_wordlist(filename: str = _DEFAULT_FILE) -> tuple[str, ...]:
    """Return the wordlist, cached for the life of the process."""
    return _load(filename)


@lru_cache(maxsize=1)
def _word_set(filename: str) -> frozenset[str]:
    return frozenset(_load(filename))


def is_word(name: str, filename: str = _DEFAULT_FILE) -> bool:
    """True if *name* is an exact entry in the bundled dictionary."""
    return name in _word_set(filename)
