"""Candidate sources and the factory that builds one from settings."""

from __future__ import annotations

from ..config import Settings
from ..lexicon import load_wordlist
from .base import CandidateSource, ValidatedSource
from .bruteforce import BruteforceSource
from .wordlist import FileSource, StdinSource, WordlistSource

__all__ = [
    "BruteforceSource",
    "CandidateSource",
    "FileSource",
    "StdinSource",
    "ValidatedSource",
    "WordlistSource",
    "build_source",
    "load_wordlist",
]


def build_source(settings: Settings) -> CandidateSource:
    """Construct the candidate source described by *settings*."""
    match settings.source:
        case "wordlist":
            return WordlistSource(length=settings.length)
        case "bruteforce":
            return BruteforceSource(length=settings.length)
        case "file":
            return FileSource(settings.names_file)
        case "stdin":
            return StdinSource()
        case _:  # pragma: no cover - pydantic constrains the literal
            raise ValueError(f"unknown source: {settings.source!r}")
