"""Candidate sources: the pluggable "what do we check next?" layer.

A source is a synchronous iterator of usernames, ordered **best-first**.
Ordering matters enormously here: the public Discord endpoint is throttled
hard enough that a full sweep of the ~1.9M possible 4-character combinations
is not reachable, so the radar must always be spending its budget on the
names most likely to be worth something.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator

from ..names import is_valid


class CandidateSource(ABC):
    """Streams candidate usernames."""

    #: Total number of candidates, or ``None`` when the stream is unbounded
    #: (stdin, or a file read at construction time) and cannot be counted.
    total: int | None

    #: Human label used in logs and ``/status``.
    label: str = "source"

    @abstractmethod
    def __iter__(self) -> Iterator[str]:
        """Yield candidate usernames in descending desirability order."""

    def __len__(self) -> int:
        if self.total is None:
            raise TypeError(f"{type(self).__name__} has no known length")
        return self.total


class ValidatedSource(CandidateSource):
    """Base class that guarantees every emitted name is syntactically valid."""

    def __iter__(self) -> Iterator[str]:
        for raw in self._raw_iter():
            name = raw.strip().lower()
            if is_valid(name):
                yield name

    @abstractmethod
    def _raw_iter(self) -> Iterator[str]:
        """Yield unvalidated strings."""
