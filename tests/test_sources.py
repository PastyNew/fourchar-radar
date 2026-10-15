"""Candidate sources: ordering, validation and totals."""

from __future__ import annotations

import itertools
from pathlib import Path

import pytest
from pydantic import ValidationError

from fourchar_radar.config import Settings
from fourchar_radar.names import is_valid
from fourchar_radar.sources import build_source
from fourchar_radar.sources.bruteforce import BruteforceSource
from fourchar_radar.sources.wordlist import FileSource, WordlistSource, load_wordlist

WEBHOOK = "https://discord.com/api/webhooks/1/t"


def test_wordlist_source_is_sorted_deduplicated_and_valid() -> None:
    source = WordlistSource(length=4)
    words = list(source)

    assert words == sorted(words)
    assert len(words) == len(set(words))
    assert source.total == len(words)
    assert all(is_valid(w) for w in words)
    assert len(words) > 500, "the bundled list should be substantial"


def test_wordlist_length_filter() -> None:
    assert all(len(w) == 5 for w in WordlistSource(length=5))


def test_load_wordlist_is_cached_and_clean() -> None:
    words = load_wordlist()
    assert load_wordlist() is words  # cached: same object
    assert all(w and not w.startswith("#") for w in words)


def test_bruteforce_total_matches_the_alphabet() -> None:
    source = BruteforceSource(length=4)
    assert source.total == 37**4
    assert len(source) == source.total


def test_bruteforce_starts_with_pure_letters() -> None:
    """The point of best-first ordering: spend the budget on the good stuff."""
    first = list(itertools.islice(iter(BruteforceSource(length=4)), 500))
    assert all(name.isalpha() for name in first)
    assert first[0] == "aaaa"


def test_bruteforce_reaches_underscore_shapes_before_the_tail() -> None:
    names = list(itertools.islice(iter(BruteforceSource(length=4)), 26**4 + 100))
    assert any("_" in n for n in names[26**4 :]), "separator shapes should follow letters"


def test_bruteforce_emits_only_valid_names() -> None:
    for name in itertools.islice(iter(BruteforceSource(length=4)), 3000):
        assert is_valid(name)


def test_bruteforce_rejects_absurd_lengths() -> None:
    with pytest.raises(ValueError):
        BruteforceSource(length=1)


def test_bruteforce_length_two_works() -> None:
    assert len(list(BruteforceSource(length=2))) >= 26**2


def test_file_source(tmp_path: Path) -> None:
    path = tmp_path / "names.txt"
    path.write_text("cool\n# a comment\n\nface\ncool\nBEAN\n", encoding="utf-8")

    source = FileSource(path)
    names = list(source)

    assert names == ["cool", "face", "bean"]  # deduped, lowercased, comments gone
    assert source.total == 3


def test_file_source_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        FileSource(tmp_path / "nope.txt")


def test_build_source_dispatch(tmp_path: Path) -> None:
    path = tmp_path / "n.txt"
    path.write_text("cool\n", encoding="utf-8")

    base = {"discord_webhook_url": WEBHOOK}
    assert isinstance(build_source(Settings(**base)), WordlistSource)  # type: ignore[arg-type]
    assert isinstance(
        build_source(Settings(source="bruteforce", **base)),
        BruteforceSource,  # type: ignore[arg-type]
    )
    assert isinstance(
        build_source(Settings(source="file", names_file=str(path), **base)),
        FileSource,  # type: ignore[arg-type]
    )


def test_build_source_rejects_file_without_path() -> None:
    """`source=file` with no path is rejected by config validation."""
    with pytest.raises(ValidationError):
        Settings(source="file", discord_webhook_url=WEBHOOK)
