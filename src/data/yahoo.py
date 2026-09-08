"""Yahoo Finance as a source of daily bars, for backtesting only.

Nothing in the report or trading path imports this module at process start -
``yfinance`` (and the pandas it drags in) is imported *inside* ``fetch``, not
at module load, so a machine with no ``yfinance`` installed can still run
``main.py`` and ``trade.py`` without ever noticing this module exists.
"""

from datetime import timedelta
from decimal import Decimal

from src.data.errors import DataUnavailableError
from src.data.sessions import last_closed_session_date
from src.strategy.bars import Bar, PriceHistory

NAME = "yahoo"


def _to_decimal(value):
    """Convert a numpy/float value to Decimal without a float artefact.

    ``Decimal(0.1)`` is ``0.1000000000000000055511151231257827021181583404541015625``
    - the float's own imprecision, made permanent. Formatting through a fixed
    number of digits first is the boundary where that stops.
    """
    if value is None:
        return None
    try:
        if value != value:  # NaN
            return None
    except TypeError:
        pass
    return Decimal(f"{float(value):.6f}")


class YahooBarSource:
    """Fetches adjusted daily OHLCV bars for one symbol at a time."""

    name = NAME

    def fetch(self, symbol, start, end):
        """Bars for ``symbol`` in ``[start, end]``, oldest first.

        Raises :class:`DataUnavailableError` on an empty result rather than
        returning a short series silently - a gap that looks like "nothing
        happened that day" is worse than one that stops the run.
        """
        try:
            import yfinance as yf
        except ImportError as exc:
            raise DataUnavailableError(
                "yfinance가 설치되어 있지 않습니다. 백테스트/시세 갱신에만 필요합니다: "
                "pip install yfinance"
            ) from exc

        # yfinance's `end` is exclusive; callers of this method mean inclusive.
        frame = yf.download(
            symbol,
            start=start,
            end=end + timedelta(days=1),
            auto_adjust=True,  # splits/dividends folded into `close` - see
            # module docstring: an unadjusted 10:1 split reads as a -90% day.
            progress=False,
            multi_level_index=False,
        )
        if frame is None or frame.empty:
            raise DataUnavailableError(f"{symbol}: yfinance에서 데이터를 받지 못했습니다.")

        # Filtered here rather than at read time so a partial bar never
        # reaches the cache in the first place - once written it would
        # outlive the session that produced it.
        cutoff = last_closed_session_date()

        bars = []
        for index, row in frame.iterrows():
            day = index.date() if hasattr(index, "date") else index
            if day > cutoff:
                continue
            open_, high, low, close = (
                _to_decimal(row.get(col)) for col in ("Open", "High", "Low", "Close")
            )
            if None in (open_, high, low, close):
                continue
            volume = _to_decimal(row.get("Volume"))
            bars.append(
                Bar(
                    date=day,
                    open=open_,
                    high=high,
                    low=low,
                    close=close,
                    volume=volume,
                )
            )
        if not bars:
            raise DataUnavailableError(f"{symbol}: 유효한 봉이 하나도 없습니다.")
        return PriceHistory(symbol, tuple(bars))
