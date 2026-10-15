"""Heuristic valuation of a free username.

**What this is:** a triage heuristic. It exists to answer "is this find worth
interrupting me for?" so the radar can rank and filter alerts — not to tell you
what a name would fetch on an auction site.

**How it works:** a set of interpretable features (is it a word? is it
pronounceable? how big a character set does it draw from?) each contribute a
bonus in USD, and the total is mapped onto a tier. Every contribution is
returned in ``Quote.reasons`` so an alert can explain itself.

Tuning lives in :class:`PriceModel`. The defaults are deliberately conservative
— they were chosen to keep the common case (a random letter combo) near the
bottom of the ladder and reserve the top tiers for things that genuinely stand
out, rather than to predict real market prices.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from functools import lru_cache

from ..lexicon import is_word
from ..models import Quote, Tier
from ..names import VOWELS, vowel_ratio

#: Characters that are routinely misread for a letter or vice versa: 0/o,
#: 1/l/i, 5/s, 2/z, 6/b/g, 8/b, 9/g/q. Only the *digit* side is listed,
#: because the digit is always the thing that hurts readability.
CONFUSABLE_DIGITS = frozenset("0125689")

#: Consecutive characters on a QWERTY row, used for the "sequence" bonus.
KEYBOARD_SEQUENCES = (
    "abcdefghijklmnopqrstuvwxyz",
    "qwertyuiop",
    "asdfghjkl",
    "zxcvbnm",
    "0123456789",
)


@dataclass(frozen=True, slots=True)
class PriceModel:
    """Tunable weights. Every field is in USD unless noted."""

    base_usd: float = 15.0
    word_bonus: float = 900.0
    pronounceable_bonus: float = 90.0
    readable_bonus: float = 70.0
    distinct_chars_bonus: float = 80.0
    double_letter_bonus: float = 35.0
    sequence_bonus: float = 160.0
    letter_only_bonus: float = 60.0
    all_vowel_penalty: float = 0.25
    consonant_clash_penalty: float = 0.6
    cap_usd: float = 250_000.0

    #: (inclusive lower bound, tier) — checked top-down.
    tiers: Sequence[tuple[float, Tier]] = field(
        default=(
            (50_000.0, Tier.LEGENDARY),
            (10_000.0, Tier.EPIC),
            (1_000.0, Tier.RARE),
            (150.0, Tier.UNCOMMON),
        )
    )

    def tier_for(self, price: float) -> Tier:
        for threshold, tier in self.tiers:
            if price >= threshold:
                return tier
        return Tier.COMMON


DEFAULT_MODEL = PriceModel()


# --------------------------------------------------------------- features
@lru_cache(maxsize=8192)
def _shape(name: str) -> tuple[float, float, float]:
    """Cheap per-name signals, computed once and cached.

    Returns ``(pronounceable, readable, distinct_ratio)``, each in ``0..1``.
    """
    # Pronounceability: needs at least one vowel and no long consonant cluster.
    vr = vowel_ratio(name)
    max_run = _max_consonant_run(name)
    has_vowel = any(c in VOWELS for c in name)
    if has_vowel and max_run <= 2 and 0.25 <= vr <= 0.75:
        pronounceable = 1.0
    elif has_vowel and max_run <= 3:
        pronounceable = 0.5
    else:
        pronounceable = 0.0

    # Readability: a digit in here is a misread waiting to happen.
    readable = 0.0 if any(c in CONFUSABLE_DIGITS or c.isdigit() for c in name) else 1.0

    return pronounceable, readable, len(set(name)) / len(name)


def _max_consonant_run(name: str) -> int:
    run = best = 0
    for ch in name:
        run = 0 if ch in VOWELS else run + 1
        best = max(best, run)
    return best


def _has_sequence(name: str) -> bool:
    """True if *name* contains a run from a keyboard row, the alphabet or 0-9.

    The minimum run is ``min(len(name), 4)``: at length 4 a 3-character match
    ("xyz") is too common to mean anything, so a full 4-character run is the
    bar.
    """
    minimum = min(len(name), 4)
    for seq in KEYBOARD_SEQUENCES:
        for size in range(len(seq), minimum - 1, -1):
            for start in range(len(seq) - size + 1):
                chunk = seq[start : start + size]
                if chunk in name or chunk[::-1] in name:
                    return True
    return False


def _has_double_letter(name: str) -> bool:
    return any(a == b for a, b in zip(name, name[1:], strict=False))


# -------------------------------------------------------------- estimate
def estimate(name: str, model: PriceModel = DEFAULT_MODEL) -> Quote:
    """Value *name* and explain the verdict."""
    reasons: list[str] = []
    price = model.base_usd

    # -- the big one -------------------------------------------------------
    if is_word(name):
        price += model.word_bonus
        reasons.append("exact English word")
    elif any(is_word(name[:i] + name[i + 1 :]) for i in range(len(name))):
        price += model.word_bonus * 0.08
        reasons.append("one letter off a real word")

    # -- shape and readability --------------------------------------------
    pronounceable, readable, distinct = _shape(name)
    if pronounceable >= 1.0:
        price += model.pronounceable_bonus
        reasons.append("easy to say out loud")
    if readable >= 1.0:
        price += model.readable_bonus
        reasons.append("no confusable glyphs")
    if distinct >= 0.75:
        price += model.distinct_chars_bonus
        reasons.append("almost no repeated characters")
    if distinct < 0.5:
        price *= 0.7
        reasons.append("many repeated characters")

    if _has_sequence(name):
        price += model.sequence_bonus
        reasons.append("contains a keyboard/character sequence")
    if _has_double_letter(name):
        price += model.double_letter_bonus
        reasons.append("has a doubled letter")

    if name.isalpha() and name.islower():
        price += model.letter_only_bonus
        reasons.append("letters only, no digits or underscores")

    # -- penalties ---------------------------------------------------------
    if name.isalpha() and all(c in VOWELS for c in name):
        price *= model.all_vowel_penalty
        reasons.append("all vowels (hard to read)")

    if _max_consonant_run(name) >= 3:
        price *= model.consonant_clash_penalty
        reasons.append("consonant cluster (hard to read)")

    final = max(1.0, min(price, model.cap_usd))
    reasons.append(f"base ${model.base_usd:.0f} for a free 4-char name")

    return Quote(
        name=name,
        price_usd=int(final),
        score=round(final, 2),
        tier=model.tier_for(final),
        reasons=tuple(reasons),
    )


def estimate_many(names: Iterable[str], model: PriceModel = DEFAULT_MODEL) -> list[Quote]:
    return [estimate(name, model) for name in names]


def explain(quote: Quote) -> str:
    """One-line human summary used in CLI output and embeds."""
    tier = quote.tier.value.upper()
    return f"{quote.name} — {quote.price_label} ({tier}): " + "; ".join(quote.reasons)
