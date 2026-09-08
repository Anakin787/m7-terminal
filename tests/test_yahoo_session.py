"""The cutoff that keeps an in-progress session's bar out of the cache.

The property under test is not "the arithmetic is right" - it is that *the
hour the batch runs at does not change what the strategy sees*. Before this
guard, a 10:10 KST run anchored on Monday's close while a 23:35 KST run
anchored on Tuesday's unfinished candle, which silently moved the rebalance
weekday and scored momentum on a partial bar.
"""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from src.data.sessions import EXCHANGE_TZ, last_closed_session_date

KST = ZoneInfo("Asia/Seoul")


def test_after_the_close_the_session_counts_as_finished():
    now = datetime(2026, 9, 1, 21, 10, tzinfo=EXCHANGE_TZ)  # ET Mon evening
    assert last_closed_session_date(now) == date(2026, 9, 1)


def test_during_the_session_the_day_is_not_yet_available():
    now = datetime(2026, 9, 1, 10, 35, tzinfo=EXCHANGE_TZ)  # ET Mon mid-session
    assert last_closed_session_date(now) == date(2026, 8, 31)


def test_the_settle_buffer_holds_the_bar_just_past_the_bell():
    at_the_bell = datetime(2026, 9, 1, 16, 1, tzinfo=EXCHANGE_TZ)
    after_buffer = datetime(2026, 9, 1, 16, 16, tzinfo=EXCHANGE_TZ)
    assert last_closed_session_date(at_the_bell) == date(2026, 8, 31)
    assert last_closed_session_date(after_buffer) == date(2026, 9, 1)


def test_both_schedules_anchor_on_the_same_session():
    """The reason this guard exists.

    KST Tue 10:10 (the old trading slot) and KST Tue 23:35 (the new one, in
    the US regular session) must both see Monday as the last completed
    session. Without the guard the second one sees Tuesday.
    """
    morning = datetime(2026, 9, 8, 10, 10, tzinfo=KST)
    evening = datetime(2026, 9, 8, 23, 35, tzinfo=KST)
    monday = date(2026, 9, 7)

    assert last_closed_session_date(morning) == monday
    assert last_closed_session_date(evening) == monday


def test_a_naive_kst_date_would_have_disagreed():
    """Guards the reasoning, not just the result.

    A tempting simpler rule - "drop bars dated today" - breaks on the morning
    run, where KST has already rolled to Tuesday while ET is still Monday
    evening and Monday's bar is both complete and the one we want.
    """
    morning = datetime(2026, 9, 8, 10, 10, tzinfo=KST)
    assert morning.date() == date(2026, 9, 8)
    assert last_closed_session_date(morning) == date(2026, 9, 7)


def test_a_weekend_run_falls_back_to_the_last_traded_day():
    """No special case needed: no bar is stamped with a Saturday."""
    saturday = datetime(2026, 9, 5, 9, 0, tzinfo=EXCHANGE_TZ)
    cutoff = last_closed_session_date(saturday)
    assert cutoff == date(2026, 9, 4)
    assert date(2026, 9, 4) < cutoff + timedelta(days=1)
