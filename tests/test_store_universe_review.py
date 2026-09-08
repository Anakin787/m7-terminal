"""Storage for the AI universe review - above all, that vetoes expire."""

from datetime import datetime, timedelta

import pytest

from src.store.repo import Store
from src.universe_review import Candidate, UniverseReview, Veto


@pytest.fixture
def store(firestore_client):
    return Store(firestore_client)


def review(**overrides):
    base = dict(
        vetoes=(
            Veto(
                symbol="AAPL",
                category="trading_halt",
                reason="거래정지",
                evidence="headline",
            ),
        ),
        candidates=(Candidate(symbol="ASML", name="ASML", reason="장비 노출"),),
    )
    base.update(overrides)
    return UniverseReview(**base)


def test_a_saved_veto_is_active_right_away(store):
    store.save_universe_review(review(), ttl_days=7)
    assert store.active_vetoes() == {"AAPL": "거래정지"}


def test_a_veto_lapses_once_its_ttl_has_passed(store):
    ts = (datetime.now() - timedelta(days=30)).isoformat()
    store.save_universe_review(review(), ttl_days=7, ts=ts)

    # Nothing renewed it, so it is gone - a batch job that stops running must
    # not leave an AI's opinion in force forever.
    assert store.active_vetoes() == {}


def test_re_raising_a_veto_pushes_its_expiry_out(store):
    old = (datetime.now() - timedelta(days=30)).isoformat()
    store.save_universe_review(review(), ttl_days=7, ts=old)
    store.save_universe_review(review(), ttl_days=7)

    assert "AAPL" in store.active_vetoes()


def test_a_veto_can_be_lifted_by_hand(store):
    store.save_universe_review(review(), ttl_days=7)
    store.clear_veto("AAPL")

    assert store.active_vetoes() == {}


def test_ttl_zero_means_the_veto_is_already_over(store):
    store.save_universe_review(review(), ttl_days=0)
    assert store.active_vetoes() == {}


def test_candidates_are_kept_per_run_not_overwritten(store):
    first = datetime(2026, 8, 26, 9, 0).isoformat()
    second = datetime(2026, 8, 27, 9, 0).isoformat()
    store.save_universe_review(review(), ts=first)
    store.save_universe_review(
        review(candidates=(Candidate(symbol="TSM", name="TSMC", reason="파운드리"),)),
        ts=second,
    )

    latest = store.latest_universe_candidates(limit=2)
    assert [row["ts"] for row in latest] == [second, first]
    assert latest[0]["candidates"][0]["symbol"] == "TSM"


def test_an_empty_review_writes_nothing(store):
    store.save_universe_review(UniverseReview())

    assert store.active_vetoes() == {}
    assert store.latest_universe_candidates() == []


# ------------------------------------------------------------- audit log


def test_audit_entries_come_back_newest_first(store):
    store.save_audit_entries(
        [
            {"detected_at": "2026-08-26T09:00:00", "category": "universe", "summary": "old"},
            {"detected_at": "2026-08-27T09:00:00", "category": "limits", "summary": "new"},
        ]
    )

    assert [e["summary"] for e in store.audit_page()["entries"]] == ["new", "old"]


def test_audit_entries_can_be_filtered_by_category(store):
    store.save_audit_entries(
        [
            {"detected_at": "2026-08-26T09:00:00", "category": "universe", "summary": "u"},
            {"detected_at": "2026-08-27T09:00:00", "category": "veto", "summary": "v"},
        ]
    )

    assert [e["summary"] for e in store.audit_page(category="veto")["entries"]] == ["v"]


def test_the_fingerprint_is_absent_until_something_records_one(store):
    # None, not {} - "never recorded" and "recorded as empty" are different
    # facts, and only the first one may be silent.
    assert store.audit_fingerprint() is None

    store.save_audit_fingerprint({"universe": {"AAPL": {"max_weight": "0.35"}}})
    assert store.audit_fingerprint()["universe"]["AAPL"]["max_weight"] == "0.35"
