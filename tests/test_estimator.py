"""Pricing heuristic.

These tests assert *ordering and invariants*, not exact numbers — the weights
are meant to be tuned, and a tuning change should not break the suite. What
must hold is that better names are never worth less than worse ones.
"""

from __future__ import annotations

import pytest

from fourchar_radar.models import Tier
from fourchar_radar.pricing.estimator import DEFAULT_MODEL, estimate, explain


def test_real_word_beats_invented_name() -> None:
    assert estimate("cool").price_usd > estimate("qzxv").price_usd


def test_dictionary_word_lands_in_a_higher_tier_than_random_noise() -> None:
    assert estimate("love").tier.rank >= Tier.UNCOMMON.rank
    assert estimate("qzxv").tier.rank <= Tier.UNCOMMON.rank


def test_words_are_priced_at_the_documented_order_of_magnitude() -> None:
    """Calibration guard: real 4-letter words should read as RARE-ish."""
    for name in ("cool", "face", "love", "best", "code"):
        quote = estimate(name)
        assert quote.price_usd >= 1_000, f"{name} priced at {quote.price_label}"
        assert quote.tier is Tier.RARE


def test_readability_penalties_lower_the_price() -> None:
    # Same letters, but one is spelled with digits a human cannot read aloud.
    assert estimate("cool").price_usd > estimate("c00l").price_usd
    assert estimate("beef").price_usd > estimate("aeio").price_usd  # consonant run vs all-vowel
    assert estimate("cafe").price_usd > estimate("aeio").price_usd


def test_repetition_lowers_the_price() -> None:
    assert estimate("bean").price_usd > estimate("bbbb").price_usd


def test_sequence_bonus_applies_at_full_length_only() -> None:
    assert any("sequence" in r for r in estimate("abcd").reasons)
    # A 3-char tail of the alphabet is too common to be worth anything.
    assert not any("sequence" in r for r in estimate("xyzq").reasons)


def test_price_is_capped_and_never_negative() -> None:
    cap = int(DEFAULT_MODEL.cap_usd)
    for name in ("love", "cool", "abcd", "qwer", "0987"):
        quote = estimate(name)
        assert 1 <= quote.price_usd <= cap


def test_every_quote_explains_itself() -> None:
    quote = estimate("cool")
    assert quote.reasons, "a quote with no reasons is not actionable"
    assert any("base $" in r for r in quote.reasons), "the base term should be stated"
    assert quote.name == "cool"
    assert quote.tier is not None


def test_explain_line_is_readable() -> None:
    line = explain(estimate("cool"))
    assert "cool" in line
    assert "RARE" in line


@pytest.mark.parametrize(
    ("price", "expected"),
    [
        (0, Tier.COMMON),
        (149, Tier.COMMON),
        (150, Tier.UNCOMMON),
        (1_000, Tier.RARE),
        (50_000, Tier.LEGENDARY),
    ],
)
def test_tier_boundaries(price: int, expected: Tier) -> None:
    assert DEFAULT_MODEL.tier_for(price) is expected


def test_estimator_is_deterministic() -> None:
    assert estimate("cool") == estimate("cool")


def test_price_label_formatting() -> None:
    from fourchar_radar.models import Quote

    assert Quote("a", 850, 850.0, Tier.UNCOMMON).price_label == "$850"
    assert Quote("a", 12_400, 12_400.0, Tier.EPIC).price_label == "$12.4k"
    assert Quote("a", 1_250_000, 1.25e6, Tier.LEGENDARY).price_label == "$1.2M"
