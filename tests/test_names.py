"""Username syntax rules — the shared contract between every layer."""

from __future__ import annotations

import pytest

from fourchar_radar.names import (
    char_classes,
    is_valid,
    normalize,
    repeated_ratio,
    vowel_ratio,
)


@pytest.mark.parametrize(
    "name",
    ["huhu", "cool", "a_b", "ab", "x1y2", "a.b", "a" * 32],
)
def test_accepts_valid(name: str) -> None:
    assert is_valid(name)


@pytest.mark.parametrize(
    "name",
    ["", "a", "a" * 33, "Hihu", "hu hu", "hi-hu", "hi@hu", "привет", "ab/c"],
)
def test_rejects_invalid(name: str) -> None:
    assert not is_valid(name)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("  Huhu  ", "huhu"),
        ("<@123>", "123"),  # mention syntax is stripped
        ("@huhu", "huhu"),
        ("cool\n", "cool"),
        ("", ""),
        ("a", ""),
        ("!!", ""),
    ],
)
def test_normalize(raw: str, expected: str) -> None:
    assert normalize(raw) == expected


def test_char_classes() -> None:
    assert char_classes("cool") == {"letters"}
    assert char_classes("a_1b") == {"letters", "digits", "sep"}
    assert char_classes("___") == {"sep"}


def test_vowel_ratio() -> None:
    assert vowel_ratio("cool") == pytest.approx(0.5)
    assert vowel_ratio("bcdg") == 0.0
    assert vowel_ratio("abcd") == pytest.approx(0.25)
    assert vowel_ratio("1234") == 1.0  # no letters -> treated as fully pronounceable


def test_repeated_ratio() -> None:
    assert repeated_ratio("abcd") == 0.0
    assert repeated_ratio("aaaa") == pytest.approx(0.75)
    assert repeated_ratio("") == 0.0
