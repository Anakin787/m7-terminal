"""Cache-first historical bars, offline-capable for reproducible backtests.

``offline=True`` never touches the network, even to fill a gap. That is the
whole reproducibility argument: two runs of the same backtest against the same
cache must produce identical results, and a run that quietly topped up today's
bar mid-backtest would not.
"""

from datetime import timedelta

from src.data.errors import DataUnavailableError
from src.data.sessions import last_closed_session_date
from src.strategy.bars import as_date


class HistoryLoader:
    """Reads :class:`~src.data.cache.BarCache`, refreshing from ``source`` on demand.

    ``staleness_days`` governs the *offline* read only - how far the tail of a
    cached series may lag the requested ``end`` before the symbol is dropped.
    It deliberately has no say over :meth:`refresh`; see that method for why.
    """

    def __init__(
        self, cache, source=None, offline=False, staleness_days=3, session_cutoff=None
    ):
        self.cache = cache
        self.source = source
        self.offline = offline
        self.staleness_days = staleness_days
        #: Injectable so tests can state a date instead of waiting for one.
        self.session_cutoff = session_cutoff or last_closed_session_date

    def load(self, symbols, start, end):
        """``{symbol: PriceHistory}`` for every symbol, from the cache alone.

        A symbol with no cached coverage of the requested range is simply
        absent from the result rather than raising - the caller (a strategy's
        cold-start check, or the backtest engine) decides whether that is
        fatal. ``offline=False`` calls :meth:`refresh` first so a live run
        picks up what changed since the cache was last touched.

        Two things that look like gaps are not, and dropping the symbol for
        either of them loses real data:

        *A later listing.* A company that IPO'd inside the window has no bars
        before it existed, and never will. Refusing it excludes exactly the
        young, fast-growing names a universe review is most likely to add -
        silently, and more so the longer the warm-up window in front of the
        backtest. It takes part from its first bar instead.

        *A window that ends on a non-trading day.* ``end`` is usually today,
        and today is often a weekend, a holiday, or a session that has not
        closed. Demanding a bar dated exactly ``end`` then drops every symbol
        in the universe at once and reports it as an empty cache. The tail is
        allowed to lag by ``staleness_days``.
        """
        if not self.offline:
            self.refresh(symbols, start, end)

        result = {}
        for symbol in symbols:
            first, last = self.cache.coverage(symbol)
            if first is None:
                continue
            if self.offline and last < as_date(end) - timedelta(days=self.staleness_days):
                # Reproducibility over convenience: a genuinely short series
                # must be loud, not filled in by a network call the caller
                # did not ask for.
                continue
            history = self.cache.bars(symbol, start, end)
            if history:
                result[symbol] = history
        return result

    def refresh(self, symbols, start, end):
        """Fetch whatever the cache is missing for each symbol.

        Returns ``{symbol: bar_count_added}``. Raises if a source was never
        configured - refreshing with no source is a configuration mistake,
        not a data gap.

        "Missing" is measured against the **exchange**, not against ``end``: a
        symbol is refetched whenever the cache stops short of the last closed
        session. The obvious-looking alternative - skip if the tail is within
        a few days of ``end`` - is what this replaced, and it was wrong in a
        way nothing reported. This is the only place that refreshes bars, so
        a skipped run leaves the cache short until the next one, and a
        tolerance of N days means the newest bar arrives up to N days late.
        Strategies anchor "today" to that last bar (``bucket_dca.evaluate``),
        so the tolerance did not delay the weekly rebalance - it *repeated*
        it: with a 3-day window the cache sat on Monday's bar through
        Wednesday and Thursday, each of those runs read "today is Monday",
        and the week's buy was proposed three times. The cadence a strategy
        runs on must come from the market's calendar, not from how often the
        cache happens to be topped up.

        A cutoff on a day the exchange did not trade (a weekend, a holiday, or
        a Monday-evening KST run, where the last closed session in New York is
        Sunday) costs one fetch attempt per symbol that returns nothing. That
        is the price of not carrying a holiday calendar, and it is paid in
        requests rather than in correctness.
        """
        if self.source is None:
            raise DataUnavailableError(
                "HistoryLoader에 source가 설정되지 않아 갱신할 수 없습니다."
            )

        cutoff = self.session_cutoff()
        added = {}
        for symbol in symbols:
            first, last = self.cache.coverage(symbol)
            fetch_start = as_date(start)
            if last is not None:
                # Already covered through the last session the exchange has
                # finished - there is nothing yet to fetch.
                if last >= cutoff:
                    added[symbol] = 0
                    continue
                fetch_start = last + timedelta(days=1)

            try:
                history = self.source.fetch(symbol, fetch_start, as_date(end))
            except DataUnavailableError:
                added[symbol] = 0
                continue

            count = self.cache.upsert(symbol, history.bars, self.source.name)
            added[symbol] = count
        return added
