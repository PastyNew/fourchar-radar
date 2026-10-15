"""Wordlist-backed candidate sources.

Real 4-letter English words are both the most requested and the most
saleable short usernames — and there are only a couple of thousand of them,
small enough that a patient scan can actually work through the list.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from ..lexicon import load_wordlist
from .base import ValidatedSource


class WordlistSource(ValidatedSource):
    """Every bundled word of the requested length."""

    def __init__(self, length: int = 4, filename: str = "words4.txt") -> None:
        self.length = length
        self.filename = filename
        self._words = tuple(w for w in load_wordlist(filename) if len(w) == length)
        self.total = len(self._words)
        self.label = f"wordlist:{filename}({length})"

    def _raw_iter(self) -> Iterator[str]:
        yield from self._words


class FileSource(ValidatedSource):
    """One name per line; blank lines and ``#`` comments ignored."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.label = f"file:{self.path}"

        if not self.path.is_file():
            raise FileNotFoundError(f"wordlist not found: {self.path}")

        seen: dict[str, None] = {}
        for line in self.path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                seen.setdefault(stripped, None)
        self._words = tuple(seen)
        self.total = len(self._words)

    def _raw_iter(self) -> Iterator[str]:
        yield from self._words


class StdinSource(ValidatedSource):
    """Names piped in from stdin. The blocking read runs off the event loop."""

    def __init__(self) -> None:
        self.label = "stdin"
        self._words = self._read()

    @staticmethod
    def _read() -> tuple[str, ...]:
        import sys
        from threading import Thread

        captured: list[str] = []
        thread = Thread(target=lambda: captured.extend(sys.stdin.read().splitlines()))
        thread.start()
        thread.join()
        return tuple(captured)

    @property
    def total(self) -> int | None:  # type: ignore[override]
        return len(self._words)

    def _raw_iter(self) -> Iterator[str]:
        yield from self._words
