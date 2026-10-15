"""Packaging regressions.

These tests exist because the bundled wordlist silently failed to load from an
installed wheel: it lived in the repo root, so a source checkout worked while
`pip install fourchar-radar` produced a package that could not build a single
candidate. They assert against `importlib.resources` specifically — the
mechanism that only exercises the packaged copy.
"""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path

import pytest

from fourchar_radar.lexicon import load_wordlist

DATA_PACKAGE = "fourchar_radar.data"


def test_wordlist_is_packaged() -> None:
    assert files(DATA_PACKAGE).joinpath("words4.txt").is_file()


def test_wordlist_loads_through_importlib_resources() -> None:
    text = files(DATA_PACKAGE).joinpath("words4.txt").read_text(encoding="utf-8")
    assert "cool" in text
    assert "face" in text


def test_load_wordlist_reads_packaged_data_not_the_repo() -> None:
    words = load_wordlist()
    assert len(words) > 500
    assert "cool" in words
    assert all(word.isalpha() and word.islower() for word in words)


def test_source_can_be_built_from_a_packaged_wordlist() -> None:
    from fourchar_radar.sources.wordlist import WordlistSource

    source = WordlistSource(length=4)
    assert source.total > 500
    assert source.total <= len(load_wordlist())
    assert "cool" in list(source)


def test_unknown_wordlist_raises_rather_than_returning_nothing() -> None:
    """A missing file must be loud: an empty source looks like 'all taken'."""
    with pytest.raises((FileNotFoundError, ModuleNotFoundError, OSError)):
        load_wordlist("definitely-not-a-real-list.txt")


def test_py_typed_marker_ships_with_the_package() -> None:
    assert files("fourchar_radar").joinpath("py.typed").is_file()


def test_installer_can_install_without_pypi() -> None:
    """The package is not on PyPI yet, so the one-liner must fall back.

    Without this the advertised `curl | sh` install fails outright for
    everyone, since `pip install fourchar-radar` finds nothing.
    """
    text = (Path(__file__).resolve().parents[1] / "install.sh").read_text()

    assert "archive/refs/heads/main.tar.gz" in text, "must have a GitHub fallback"
    assert "not on PyPI" in text
    # And it must actually verify the import succeeded, not just trust pip.
    assert "import fourchar_radar" in text


def test_installer_points_at_the_right_repository() -> None:
    text = (Path(__file__).resolve().parents[1] / "install.sh").read_text()
    assert "PastyNew/fourchar-radar" in text
    assert "your-org" not in text
