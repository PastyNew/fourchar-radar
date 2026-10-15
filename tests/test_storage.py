"""SQLite skip-set and findings history."""

from __future__ import annotations

from datetime import timedelta

from fourchar_radar.models import Quote, Tier, Verdict, utcnow
from fourchar_radar.storage import Store


def _verdict(name: str, available: bool) -> Verdict:
    return Verdict(name=name, available=available, checked_at=utcnow(), latency_ms=10.0)


def _quote(name: str, price: int, tier: Tier = Tier.RARE) -> Quote:
    return Quote(name=name, price_usd=price, score=float(price), tier=tier, reasons=("a", "b"))


def test_fresh_store_is_empty(store: Store) -> None:
    assert store.summary() == {"total": 0, "free": 0, "notified": 0}
    assert store.findings() == []
    assert not store.is_checked("cool")


def test_record_then_skip_set(store: Store) -> None:
    store.record(_verdict("cool", False), None)
    assert store.is_checked("cool")
    assert not store.is_checked("face")


def test_findings_sorted_most_valuable_first(store: Store) -> None:
    store.record(_verdict("cheap", True), _quote("cheap", 200, Tier.UNCOMMON))
    store.record(_verdict("rich", True), _quote("rich", 9_000, Tier.EPIC))
    store.record(_verdict("mid", True), _quote("mid", 2_000, Tier.RARE))

    top = [f.name for f in store.findings()]
    assert top == ["rich", "mid", "cheap"]


def test_findings_respect_the_tier_floor(store: Store) -> None:
    store.record(_verdict("cheap", True), _quote("cheap", 200, Tier.UNCOMMON))
    store.record(_verdict("rich", True), _quote("rich", 9_000, Tier.EPIC))

    assert [f.name for f in store.findings(min_tier=Tier.EPIC)] == ["rich"]
    assert len(store.findings(min_tier=Tier.UNCOMMON)) == 2


def test_taken_names_never_appear_in_findings(store: Store) -> None:
    store.record(_verdict("cool", False), None)
    assert store.findings() == []


def test_recheck_that_finds_it_taken_clears_the_stale_price(store: Store) -> None:
    """A free name can be taken by someone else between runs."""
    store.record(_verdict("cool", True), _quote("cool", 5_000))
    assert len(store.findings()) == 1

    store.record(_verdict("cool", False), None)
    assert store.findings() == []
    assert store.summary()["total"] == 1


def test_record_is_upsert_not_append(store: Store) -> None:
    store.record(_verdict("cool", False), None)
    store.record(_verdict("cool", True), _quote("cool", 1_000))
    assert store.summary()["total"] == 1
    assert store.summary()["free"] == 1


def test_notification_tracking(store: Store) -> None:
    store.record(_verdict("cool", True), _quote("cool", 1_000))
    assert not store.was_notified("cool")
    assert [f.name for f in store.unreported(min_tier=Tier.COMMON)] == ["cool"]

    store.mark_notified("cool")
    assert store.was_notified("cool")
    assert store.unreported(min_tier=Tier.COMMON) == []


def test_notify_flag_is_cleared_when_the_name_is_lost(store: Store) -> None:
    store.record(_verdict("cool", True), _quote("cool", 1_000))
    store.mark_notified("cool")
    store.record(_verdict("cool", False), None)
    assert not store.was_notified("cool")


def test_counts_by_tier(store: Store) -> None:
    store.record(_verdict("a", True), _quote("a", 1_000, Tier.RARE))
    store.record(_verdict("b", True), _quote("b", 2_000, Tier.RARE))
    store.record(_verdict("c", True), _quote("c", 9_000, Tier.EPIC))
    assert store.counts_by_tier() == {"rare": 2, "epic": 1}


def test_prune_only_removes_old_taken_names(store: Store) -> None:
    old = _verdict("taken_old", False)
    old = Verdict(name=old.name, available=False, checked_at=utcnow() - timedelta(days=40))
    store.record(old, None)
    store.record(_verdict("free_old", True), _quote("free_old", 1_000))

    assert store.prune_taken(older_than_days=30) == 1
    assert [f.name for f in store.findings()] == ["free_old"]


def test_prune_respects_the_age_window(store: Store) -> None:
    store.record(_verdict("fresh_taken", False), None)
    assert store.prune_taken(older_than_days=30) == 0
    assert store.summary()["total"] == 1


def test_meta_roundtrip(store: Store) -> None:
    assert store.get_meta("nope") is None
    store.set_meta("last_scan", "wordlist")
    assert store.get_meta("last_scan") == "wordlist"
    store.set_meta("last_scan", "bruteforce")
    assert store.get_meta("last_scan") == "bruteforce"


def test_record_many_is_batched(store: Store) -> None:
    pairs = [(_verdict(f"n{i}", i % 2 == 0), _quote(f"n{i}", 1_000)) for i in range(50)]
    store.record_many(pairs)
    assert store.summary()["total"] == 50
    assert store.summary()["free"] == 25


def test_record_many_with_no_rows_is_a_noop(store: Store) -> None:
    store.record_many([])
    assert store.summary()["total"] == 0


def test_pending_recheck_skips_fresh_records(store: Store) -> None:
    store.record(_verdict("cool", True), _quote("cool", 1_000))
    # Just checked, so nothing is due for a re-check yet.
    assert store.pending_recheck(older_than_days=7) == []


def test_pending_recheck_returns_stale_records(store: Store) -> None:
    stale = Verdict(name="cool", available=True, checked_at=utcnow() - timedelta(days=9))
    store.record(stale, _quote("cool", 1_000))
    assert store.pending_recheck(older_than_days=7) == ["cool"]


def test_clear(store: Store) -> None:
    store.record(_verdict("cool", True), _quote("cool", 1_000))
    store.clear()
    assert store.summary()["total"] == 0


def test_close_is_idempotent(tmp_path) -> None:  # type: ignore[no-untyped-def]
    s = Store(tmp_path / "x.db")
    s.close()
    s.close()
