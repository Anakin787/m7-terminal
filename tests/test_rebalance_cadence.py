"""How often the weekly rebalance actually fires, across a week of real runs.

The pieces this spans were each correct on their own: the loader tops up a
stale cache, and ``_is_rebalance_session`` answers "is this session the week's
buying day?". The bug lived in the seam. Only ``trade.py`` refreshes bars, so
whatever the loader decides to skip is what every strategy then calls "today" -
and a refresh rule expressed in days-since-``end`` let the cache sit on
Monday's bar until Friday, so three consecutive runs each read Monday and
each proposed the week's buy.

Hence a test at the level the defect existed at: step a whole week of
scheduled runs and count the firings. Neither module alone can fail this.
"""

from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from src.data.cache import BarCache
from src.data.loader import HistoryLoader
from src.data.sessions import last_closed_session_date
from src.strategy.bars import Bar, PriceHistory
from src.strategy.bucket_dca import BucketDcaStrategy

KST = ZoneInfo("Asia/Seoul")

#: The trading job's schedule - KST Mon-Fri 23:35 (RUNBOOK 6-3).
RUN_TIME = (23, 35)

BENCHMARK = "QQQ"


def _sessions(first, last):
    """Every weekday in ``[first, last]`` - a market with no holidays.

    A holiday would only make the week quieter, and the make-up rule that
    covers it has its own tests in ``test_momentum_dca``. What is under test
    here is the *repeat*, which needs an ordinary week to show up.
    """
    day, out = first, []
    while day <= last:
        if day.weekday() < 5:
            out.append(day)
        day += timedelta(days=1)
    return out


def _bar(day):
    close = Decimal("100")
    return Bar(date=day, open=close, high=close, low=close, close=close,
               volume=Decimal("1000"))


@pytest.fixture
def market(tmp_path):
    """A cache seeded up to the Friday before the week under test."""
    sessions = _sessions(date(2026, 6, 1), date(2026, 9, 18))
    cache = BarCache(tmp_path / "bars.db")
    cache.upsert(
        BENCHMARK,
        [_bar(d) for d in sessions if d <= date(2026, 9, 11)],
        source="test",
    )

    class Source:
        """Serves any session the exchange has actually finished."""

        name = "fake"

        def __init__(self):
            self.cutoff = None

        def fetch(self, symbol, start, end):
            from src.data.errors import DataUnavailableError

            bars = [
                _bar(d) for d in sessions if start <= d <= min(end, self.cutoff)
            ]
            if not bars:
                raise DataUnavailableError(f"{symbol}: nothing new")
            return PriceHistory(symbol, tuple(bars))

    return cache, Source()


def _run_week(market, session_cutoff):
    """Step Mon-Fri of 2026-09-14, returning the dates each run read as today.

    ``_is_rebalance_session`` consults ``ctx`` only when ``today`` is not a
    date, which it always is here - hence the ``None``.
    """
    cache, source = market
    strategy = BucketDcaStrategy()
    fired = []
    seen = []

    for offset in range(5):
        run_day = date(2026, 9, 14) + timedelta(days=offset)
        now = datetime(run_day.year, run_day.month, run_day.day, *RUN_TIME, tzinfo=KST)
        source.cutoff = last_closed_session_date(now)

        loader = HistoryLoader(
            cache, source=source, session_cutoff=session_cutoff(now)
        )
        start = run_day - timedelta(days=400)
        history = loader.load([BENCHMARK], start, run_day).get(BENCHMARK)

        today = history.last_date
        seen.append(today)
        if strategy._is_rebalance_session(None, today, history, strategy.params):
            fired.append(run_day)

    return seen, fired


def test_the_weekly_rebalance_fires_once_a_week(market):
    seen, fired = _run_week(market, lambda now: (lambda: last_closed_session_date(now)))

    # Each run reads the previous session, so "today" advances every day
    # instead of sticking. The KST Monday run reads Friday: at 23:35 KST it
    # is 10:35 in New York and Monday's bar does not exist yet.
    assert seen == [
        date(2026, 9, 11),  # Mon KST -> Fri
        date(2026, 9, 14),  # Tue KST -> Mon
        date(2026, 9, 15),
        date(2026, 9, 16),
        date(2026, 9, 17),
    ]
    assert fired == [date(2026, 9, 15)]  # the KST Tuesday run, once


def test_a_days_since_end_rule_fires_it_three_times(market):
    """The old rule, kept as an executable account of what went wrong.

    Not a guard on current behaviour - a demonstration that the seam is
    where the bug was, so a future "let's not refetch so often" change has
    the consequence written down rather than rediscovered in an account.
    """

    def stale_cutoff(now):
        # `last <= end - 3 days` skips, i.e. anything on or after this date
        # counts as fresh enough to leave alone.
        return lambda: now.date() - timedelta(days=3)

    seen, fired = _run_week(market, stale_cutoff)

    # Monday's bar is picked up on Tuesday and then held for three runs,
    # because for three days the cache is "within 3 days of end". The Friday
    # run falls outside the window again and moves on to Thursday - which is
    # the tell: what the strategy called "today" was tracking the refresh
    # cadence, not the calendar.
    assert seen == [
        date(2026, 9, 11),
        date(2026, 9, 14),
        date(2026, 9, 14),
        date(2026, 9, 14),
        date(2026, 9, 17),
    ]
    assert fired == [
        date(2026, 9, 15),
        date(2026, 9, 16),
        date(2026, 9, 17),
    ]
