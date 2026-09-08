"""When the exchange last finished a session.

Split out of :mod:`src.data.yahoo` because two layers need the answer and
only one of them is about Yahoo. The bar source uses it to keep an
in-progress session's partial bar out of the cache; the cache loader uses it
to decide whether the cache is actually behind the market. The loader is
deliberately source-agnostic - ``source`` is injected, and the backtest and
the strategy lab pass a different one or none at all - so it must not import
a concrete source's module to learn a calendar fact.
"""

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

#: Every symbol this project trades is US-listed, so "has this session
#: closed?" is a question about one exchange calendar. A non-US name would
#: need a per-symbol timezone here rather than this constant.
EXCHANGE_TZ = ZoneInfo("America/New_York")

#: The US regular session close, plus room for Yahoo to finish writing the
#: bar it stamps with that date - it settles a few minutes after the bell,
#: not on it.
REGULAR_CLOSE = time(16, 0)
SETTLE_BUFFER = timedelta(minutes=15)


def last_closed_session_date(now=None):
    """The most recent date whose US regular session has finished.

    A daily bar for a session still in progress is a *partial* bar: its close
    is the last trade so far, not the day's close. That matters here more than
    it looks, because strategies anchor their notion of "today" to the last
    bar's date (``bucket_dca.evaluate``). Let a partial bar in and the
    rebalance weekday shifts by a day and momentum is scored on an unfinished
    candle - and whether that happens depends on *what time of day the batch
    runs*, which is not a property a strategy should have.

    Weekends and holidays need no special case *as a cutoff*: no bar carries a
    date the exchange did not trade, so a cutoff landing on one filters
    nothing. As a freshness target they cost one wasted fetch attempt - see
    ``HistoryLoader.refresh``.
    """
    now = datetime.now(EXCHANGE_TZ) if now is None else now.astimezone(EXCHANGE_TZ)
    settled = datetime.combine(now.date(), REGULAR_CLOSE, EXCHANGE_TZ) + SETTLE_BUFFER
    return now.date() if now >= settled else now.date() - timedelta(days=1)
