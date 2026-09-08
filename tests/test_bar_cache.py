"""The bar cache and the offline-capable loader built on top of it."""

from datetime import date
from decimal import Decimal

import pytest

from src.data.cache import BarCache
from src.data.errors import DataUnavailableError
from src.data.loader import HistoryLoader
from src.strategy.bars import Bar, PriceHistory


def bar(day, close, volume="1000"):
    close = Decimal(str(close))
    return Bar(
        date=day,
        open=close,
        high=close,
        low=close,
        close=close,
        volume=Decimal(volume),
    )


class FakeSource:
    """A source whose fetches are scripted, so tests never touch a network."""

    name = "fake"

    def __init__(self, series=None, calls=None):
        self.series = series or {}
        self.calls = calls if calls is not None else []

    def fetch(self, symbol, start, end):
        self.calls.append((symbol, start, end))
        if symbol not in self.series:
            raise DataUnavailableError(f"{symbol}: no data")
        bars = [b for b in self.series[symbol] if start <= b.date <= end]
        if not bars:
            raise DataUnavailableError(f"{symbol}: empty range")
        return PriceHistory(symbol, tuple(bars))


class RaisingSource:
    """A source that must never be called - proves offline mode is honoured."""

    name = "raising"

    def fetch(self, symbol, start, end):
        raise AssertionError("offline=True must never call the source")


@pytest.fixture
def cache(tmp_path):
    return BarCache(tmp_path / "bars.db")


def test_decimal_round_trips_exactly_through_text(cache):
    original = bar(date(2026, 1, 2), "123.456789")
    cache.upsert("QQQ", [original], source="test")
    restored = cache.bars("QQQ").last()
    assert restored.close == original.close
    assert isinstance(restored.close, Decimal)


def test_upsert_is_idempotent_per_symbol_and_date(cache):
    cache.upsert("QQQ", [bar(date(2026, 1, 2), "100")], source="test")
    cache.upsert("QQQ", [bar(date(2026, 1, 2), "999")], source="test-again")
    history = cache.bars("QQQ")
    assert len(history) == 1
    assert history.last().close == Decimal("999")


def test_coverage_widens_across_upserts(cache):
    cache.upsert("QQQ", [bar(date(2026, 1, 2), "100")], source="test")
    cache.upsert("QQQ", [bar(date(2026, 1, 5), "100")], source="test")
    first, last = cache.coverage("QQQ")
    assert (first, last) == (date(2026, 1, 2), date(2026, 1, 5))


def test_bars_filters_to_the_requested_range(cache):
    for day in range(1, 6):
        cache.upsert("QQQ", [bar(date(2026, 1, day), "100")], source="test")
    history = cache.bars("QQQ", start=date(2026, 1, 2), end=date(2026, 1, 4))
    assert history.dates == (date(2026, 1, 2), date(2026, 1, 3), date(2026, 1, 4))


# ------------------------------------------------------------- HistoryLoader


def test_offline_never_calls_the_source(cache):
    cache.upsert("QQQ", [bar(date(2026, 1, 2), "100")], source="test")
    loader = HistoryLoader(cache, source=RaisingSource(), offline=True)
    # Would raise AssertionError if the source were touched.
    loader.load(["QQQ"], date(2026, 1, 1), date(2026, 1, 10))


def test_offline_gap_is_silently_skipped_not_filled(cache):
    cache.upsert("QQQ", [bar(date(2026, 1, 2), "100")], source="test")
    loader = HistoryLoader(cache, source=RaisingSource(), offline=True)
    result = loader.load(["QQQ"], date(2026, 1, 1), date(2026, 1, 10))
    assert "QQQ" not in result


def test_a_symbol_listed_after_the_window_opens_still_takes_part(cache):
    """A 2021 IPO has no 2016 bars and never will - that is not a gap.

    Dropping it excludes exactly the young names a universe review adds, and
    a long warm-up window in front of a backtest makes it happen more often.
    """
    cache.upsert("QQQ", [bar(date(2026, 1, d), "100") for d in range(1, 11)], source="test")
    cache.upsert("NEW", [bar(date(2026, 1, d), "50") for d in range(6, 11)], source="test")
    loader = HistoryLoader(cache, source=RaisingSource(), offline=True)

    result = loader.load(["QQQ", "NEW"], date(2026, 1, 1), date(2026, 1, 10))

    assert "NEW" in result
    assert result["NEW"].dates[0] == date(2026, 1, 6)


def test_a_window_ending_on_a_closed_session_keeps_the_universe(cache):
    """``end`` is usually today, and today is often a Saturday.

    Demanding a bar dated exactly ``end`` drops every symbol at once and
    reports it as an empty cache - the whole universe vanishing because the
    market was shut.
    """
    cache.upsert("QQQ", [bar(date(2026, 1, d), "100") for d in range(1, 10)], source="test")
    loader = HistoryLoader(cache, source=RaisingSource(), offline=True, staleness_days=3)

    result = loader.load(["QQQ"], date(2026, 1, 1), date(2026, 1, 10))

    assert "QQQ" in result


def test_a_series_that_stops_well_before_the_window_ends_is_still_dropped(cache):
    """The tolerance above is for a closed session, not for a dead feed."""
    cache.upsert("QQQ", [bar(date(2026, 1, d), "100") for d in range(1, 4)], source="test")
    loader = HistoryLoader(cache, source=RaisingSource(), offline=True, staleness_days=3)

    assert "QQQ" not in loader.load(["QQQ"], date(2026, 1, 1), date(2026, 1, 31))


def test_refresh_raises_without_a_configured_source(cache):
    loader = HistoryLoader(cache, source=None, offline=False)
    with pytest.raises(DataUnavailableError):
        loader.refresh(["QQQ"], date(2026, 1, 1), date(2026, 1, 10))


def test_refresh_fetches_missing_range_and_populates_the_cache(cache):
    series = {"QQQ": [bar(date(2026, 1, d), "100") for d in range(1, 11)]}
    source = FakeSource(series)
    loader = HistoryLoader(cache, source=source, session_cutoff=lambda: date(2026, 1, 10))
    added = loader.refresh(["QQQ"], date(2026, 1, 1), date(2026, 1, 10))
    assert added["QQQ"] == 10
    assert len(cache.bars("QQQ")) == 10


def test_refresh_only_fetches_the_gap_since_last_coverage(cache):
    cache.upsert("QQQ", [bar(date(2026, 1, 1), "100")], source="test")
    series = {"QQQ": [bar(date(2026, 1, d), "100") for d in range(1, 6)]}
    source = FakeSource(series)
    loader = HistoryLoader(cache, source=source, session_cutoff=lambda: date(2026, 1, 5))
    loader.refresh(["QQQ"], date(2026, 1, 1), date(2026, 1, 5))
    fetched_symbol, fetched_start, fetched_end = source.calls[0]
    assert fetched_start == date(2026, 1, 2)  # the day after existing coverage


def test_refresh_skips_a_symbol_already_at_the_last_closed_session(cache):
    cache.upsert("QQQ", [bar(date(2026, 1, 5), "100")], source="test")
    loader = HistoryLoader(
        cache, source=RaisingSource(), session_cutoff=lambda: date(2026, 1, 5)
    )
    added = loader.refresh(["QQQ"], date(2026, 1, 1), date(2026, 1, 6))
    assert added["QQQ"] == 0


def test_refresh_fetches_a_cache_one_session_behind_however_recent_it_is(cache):
    """The regression this rule exists for.

    The old rule skipped a symbol whose tail was within ``staleness_days`` of
    ``end``, which is a statement about the request rather than about the
    market. Since this is the only thing that refreshes bars, the cache then
    sat on Monday's bar all week, strategies read "today is Monday" on
    Tuesday, Wednesday and Thursday alike, and the weekly rebalance fired
    three times instead of once.
    """
    cache.upsert("QQQ", [bar(date(2026, 1, 5), "100")], source="test")
    series = {"QQQ": [bar(date(2026, 1, d), "100") for d in range(5, 7)]}
    source = FakeSource(series)
    loader = HistoryLoader(cache, source=source, session_cutoff=lambda: date(2026, 1, 6))

    added = loader.refresh(["QQQ"], date(2026, 1, 1), date(2026, 1, 6))

    assert added["QQQ"] == 1
    assert source.calls[0][1] == date(2026, 1, 6)  # only the missing tail
    assert cache.coverage("QQQ")[1] == date(2026, 1, 6)


def test_the_offline_staleness_window_does_not_govern_refresh(cache):
    """``staleness_days`` is about reading an offline cache, nothing more.

    A generous window used to mean a stale live cache; it must now mean
    nothing at all to the fetch decision.
    """
    cache.upsert("QQQ", [bar(date(2026, 1, 5), "100")], source="test")
    series = {"QQQ": [bar(date(2026, 1, 6), "100")]}
    source = FakeSource(series)
    loader = HistoryLoader(
        cache,
        source=source,
        staleness_days=30,
        session_cutoff=lambda: date(2026, 1, 6),
    )

    assert loader.refresh(["QQQ"], date(2026, 1, 1), date(2026, 1, 6))["QQQ"] == 1


def test_a_symbol_the_source_cannot_fetch_is_absent_not_fatal(cache):
    loader = HistoryLoader(
        cache, source=FakeSource({}), session_cutoff=lambda: date(2026, 1, 10)
    )
    added = loader.refresh(["NOPE"], date(2026, 1, 1), date(2026, 1, 10))
    assert added["NOPE"] == 0
    assert "NOPE" not in loader.load(["NOPE"], date(2026, 1, 1), date(2026, 1, 10))
