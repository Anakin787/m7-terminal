"""Reading the world once, into a StrategyContext.

This is the only place in the trading path that touches the network. Below
it, the strategy and the risk gate are pure functions of what this builds -
so a bad decision can always be reproduced by rebuilding the same context.

The failure policy is the opposite of the report pipeline's. There, a lookup
that fails degrades the report (``PortfolioService._collect_warnings`` skips
the symbol). Here, a lookup that fails leaves the field *absent*, and the
risk gate in strict mode reads an absent field as "cannot verify" and
rejects. A failed read must never widen what is permitted.
"""

from datetime import datetime

from src.execution.risk import kill_switch_active
from src.models import to_decimal
from src.strategy.base import MarketSession, StrategyContext, session_date_of
from src.toss.calendar import live_session, regular_window
from src.toss.errors import TossError

#: Fields Toss has used for the sellable figure.
_SELLABLE_KEYS = ("sellableQuantity", "quantity", "sellable")
_LOW_KEYS = ("lowerLimit", "lowerLimitPrice", "minPrice", "lower")
_HIGH_KEYS = ("upperLimit", "upperLimitPrice", "maxPrice", "upper")
_PRICE_KEYS = ("close", "price", "last", "lastPrice", "currentPrice")


def _pick(item, keys):
    if not isinstance(item, dict):
        return None
    for key in keys:
        value = to_decimal(item.get(key))
        if value is not None:
            return value
    return None


def build_context(
    service,
    store,
    symbols=(),
    now=None,
    kill_switch_path="KILL_SWITCH",
    snapshot=None,
    history=None,
    recent=(),
):
    """Assemble the context a strategy and the risk gate both read.

    ``service`` is a :class:`~src.pipeline.PortfolioService`, reused so the
    trading run reads the portfolio exactly the way the report does - and
    through the same cached OAuth token, which matters because Toss allows
    one valid token per client.
    """
    # Aware, because the calendar hands back aware datetimes and the risk
    # gate compares now against a session close. Mixing the two raises
    # TypeError at the comparison rather than quietly misbehaving, but only
    # if something reaches that line - so they are matched here instead.
    now = now or datetime.now().astimezone()

    if snapshot is None:
        snapshot = service.snapshot()

    held = [p.symbol for p in snapshot.positions if p.symbol]
    wanted = list(dict.fromkeys([*held, *(symbols or [])]))

    return StrategyContext(
        now=now,
        snapshot=snapshot,
        prices=_prices(service, wanted),
        buying_power=_buying_power(snapshot),
        sellable=_sellable(service, held),
        price_limits=_price_limits(service, wanted),
        sessions=_sessions(service, now),
        # Keyed on the session, not the clock: the limits are limits on a
        # trading day, and this job runs 25 minutes before the wall-clock
        # date turns over. session_date_of returns None with no history
        # loaded, which falls back to the old behaviour.
        daily_usage=store.daily_usage(
            day=now.strftime("%Y-%m-%d"),
            session_date=session_date_of(history or {}),
        ),
        kill_switch=kill_switch_active(kill_switch_path, store=store),
        blocked_symbols=_blocked_symbols(store, now),
        history=history or {},
        recent=tuple(recent),
    )


def _blocked_symbols(store, now):
    """Unexpired AI vetoes, or none at all if they cannot be read.

    The one place in this module where a failed read widens what is
    permitted, and it does so knowingly: a veto is an extra restriction on top
    of the mechanical limits, so losing it degrades the run to exactly the
    behaviour of every run before this feature existed. Refusing to trade
    because an advisory list was unavailable would be the worse failure.
    """
    try:
        return store.active_vetoes(now)
    except Exception as exc:  # noqa: BLE001 - advisory data, never fatal
        print(f"경고: AI 보류 목록을 읽지 못했습니다 ({exc}) - 보류 없이 진행합니다.")
        return {}


def _prices(service, symbols):
    if not symbols:
        return {}
    try:
        raw = service.market.prices(symbols) or {}
    except TossError:
        return {}
    prices = {}
    for symbol, item in raw.items():
        value = _pick(item, _PRICE_KEYS)
        if value is not None:
            prices[symbol] = value
    return prices


def _buying_power(snapshot):
    """Reuse the figures the snapshot already carries, as Decimal.

    ``PortfolioService`` stores these as the raw API strings, and a currency
    it could not read is left as None - which is dropped here so the gate
    sees an absent key and treats it as unverifiable.
    """
    powers = {}
    for currency, value in (snapshot.buying_power or {}).items():
        parsed = to_decimal(value)
        if parsed is not None:
            powers[currency] = parsed
    return powers


def _sellable(service, symbols):
    """One lookup per held symbol, cached in the context for the whole run.

    ORDER_INFO allows 6 requests a second and only 3 between 09:00 and 09:10,
    so this is read once rather than polled (design 2.4).
    """
    result = {}
    for symbol in symbols:
        try:
            raw = service.account.sellable_quantity(symbol) or {}
        except TossError:
            continue
        value = _pick(raw, _SELLABLE_KEYS)
        if value is not None:
            result[symbol] = value
    return result


def _price_limits(service, symbols):
    if not symbols:
        return {}
    try:
        raw = service.market.price_limits(symbols) or {}
    except TossError:
        return {}

    bands = {}
    for symbol, item in raw.items():
        low, high = _pick(item, _LOW_KEYS), _pick(item, _HIGH_KEYS)
        # A half-known band is still useful - one side can still catch a bad
        # price - but a band with neither side is just noise.
        if low is not None or high is not None:
            bands[symbol] = (low, high)
    return bands


def _sessions(service, now):
    """Turn the calendar payload into what the risk gate actually asks."""
    try:
        status = service.market_status() or {}
    except TossError:
        return {}

    sessions = {}
    for country, calendar in status.items():
        if not calendar:
            continue  # absent, so strict mode refuses to trade this market
        _, close = regular_window(calendar)
        sessions[country] = MarketSession(
            country=country,
            is_open=live_session(calendar, now) == "regular",
            regular_close=close,
        )
    return sessions
