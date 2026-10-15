"""Exhaustive generation of every syntactically valid short username.

The full space for length 4 is 37^4 = 1,874,161 names. At any realistic
request rate that is not exhaustively scannable, so this source is ordered
**best-first** and the scanner is expected to be interrupted and resumed
(the SQLite queue makes that lossless).

Ordering strategy: group candidates by how "clean" they look, emitting the
cleanest shapes first.

  1. all letters, no digits/separators  (26^4)
  2. letters + exactly one underscore
  3. letters + exactly one digit
  4. everything else
"""

from __future__ import annotations

import itertools
from collections.abc import Iterator

from ..names import ALPHABET
from .base import ValidatedSource

LETTERS = "abcdefghijklmnopqrstuvwxyz"
DIGITS = "0123456789"


class BruteforceSource(ValidatedSource):
    """Every combination over *alphabet*, grouped by desirability."""

    def __init__(
        self,
        length: int = 4,
        alphabet: str = ALPHABET,
        *,
        best_first: bool = True,
    ) -> None:
        if length < 2:
            raise ValueError("length must be >= 2 (Discord rejects shorter names)")
        self.length = length
        self.alphabet = alphabet
        self.best_first = best_first
        self.total = len(alphabet) ** length
        self.label = f"bruteforce:{length}x{len(alphabet)}"

    def _raw_iter(self) -> Iterator[str]:
        if self.best_first:
            yield from self._by_shape()
        else:
            yield from map("".join, itertools.product(self.alphabet, repeat=self.length))

    def _by_shape(self) -> Iterator[str]:
        n = self.length
        letters, digits = LETTERS, DIGITS
        seps = [c for c in self.alphabet if c not in LETTERS and c not in DIGITS]

        # Shape 1: pure letters.
        yield from map("".join, itertools.product(letters, repeat=n))

        # Shape 2: letters with exactly one separator ('_').
        for sep in seps:
            for pos in range(n):
                for filler in map("".join, itertools.product(letters, repeat=n - 1)):
                    yield filler[:pos] + sep + filler[pos:]

        # Shape 3: letters with exactly one digit.
        for pos in range(n):
            for digit in digits:
                for filler in map("".join, itertools.product(letters, repeat=n - 1)):
                    yield filler[:pos] + digit + filler[pos:]

        # Shape 4: everything else, appended in whatever order it falls out.
        # Overlaps shapes 1-3; the scanner's queue dedups by name, so these
        # repeats cost a cheap SELECT and never a network request.
        yield from map("".join, itertools.product(self.alphabet, repeat=n))

    def __len__(self) -> int:
        return int(len(self.alphabet) ** self.length)

    def estimate_bytes(self) -> int:
        """Rough memory cost if someone materialises the whole stream."""
        return len(self) * (self.length + 49)
