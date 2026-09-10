"""The strategy layer: state in, signals out.

A :class:`Strategy` receives a :class:`StrategyContext` - positions, prices,
account facts, a clock - and returns :class:`Signal` objects. It does no I/O
at all. That restriction is the point: the same ``evaluate`` that runs against
this morning's live context can be replayed over a historical one, so a
strategy can be backtested and unit-tested without a broker, a network, or a
clock that only moves forwards.

A Signal is a *proposal*, not an order. Nothing here checks whether the
account can afford it, whether the market is open, or whether the price is
inside today's band - that is :mod:`src.execution.risk`'s job, and keeping the
two apart is what lets a strategy be written without knowing the risk rules.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from src.models import ZERO

SIDE_BUY = "BUY"
SIDE_SELL = "SELL"
SIDES = frozenset({SIDE_BUY, SIDE_SELL})

ORDER_LIMIT = "LIMIT"
ORDER_MARKET = "MARKET"
ORDER_TYPES = frozenset({ORDER_LIMIT, ORDER_MARKET})


class SignalError(ValueError):
    """A strategy produced a signal that could never become a legal order."""


@dataclass(frozen=True)
class Signal:
    """One strategy's proposal to trade, with the reason it was proposed.

    ``reason`` is required rather than optional. The audit trail in section 3.3
    of the design exists to answer "why did we buy this", and a reason captured
    at the moment of the decision is the only one that is actually true - a
    reason reconstructed afterwards from the price chart is a story.
    """

    strategy: str
    symbol: str
    side: str
    reason: str

    order_type: str = ORDER_LIMIT

    #: Exactly one of these is set. ``quantity`` is share count; ``amount`` maps
    #: to the API's ``orderAmount`` (buy N won/dollars worth, letting the broker
    #: work out the shares).
    quantity: Decimal | None = None
    amount: Decimal | None = None

    #: Required for LIMIT, meaningless for MARKET.
    limit_price: Decimal | None = None

    currency: str = "KRW"

    #: Where the OCO bracket in step [9] gets its two legs. Carried on the
    #: signal because the strategy is what knows the thesis' invalidation
    #: point; the executor only registers what it is told.
    stop_loss_price: Decimal | None = None
    take_profit_price: Decimal | None = None

    #: Free-form strategy state - indicator values, thresholds - persisted as
    #: JSON so a past decision can be re-read with the numbers that drove it.
    meta: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.side not in SIDES:
            raise SignalError(f"side는 {sorted(SIDES)} 중 하나여야 합니다: {self.side!r}")
        if self.order_type not in ORDER_TYPES:
            raise SignalError(
                f"order_type은 {sorted(ORDER_TYPES)} 중 하나여야 합니다: {self.order_type!r}"
            )
        if not self.symbol:
            raise SignalError("symbol이 비어 있습니다.")
        if not (self.reason or "").strip():
            raise SignalError("reason은 필수입니다 - 감사 로그가 비면 추적이 불가능합니다.")

        # The API rejects a body carrying both, and silently trading the wrong
        # one of the two is worse than failing here.
        if (self.quantity is None) == (self.amount is None):
            raise SignalError(
                "quantity와 amount 중 정확히 하나만 지정해야 합니다 "
                f"(quantity={self.quantity}, amount={self.amount})."
            )
        for label, value in (("quantity", self.quantity), ("amount", self.amount)):
            if value is not None and value <= ZERO:
                raise SignalError(f"{label}는 0보다 커야 합니다: {value}")

        if self.order_type == ORDER_LIMIT:
            if self.limit_price is None or self.limit_price <= ZERO:
                raise SignalError("지정가 주문에는 0보다 큰 limit_price가 필요합니다.")

        if self.stop_loss_price is not None and self.stop_loss_price <= ZERO:
            raise SignalError("stop_loss_price는 0보다 커야 합니다.")
        if self.take_profit_price is not None and self.take_profit_price <= ZERO:
            raise SignalError("take_profit_price는 0보다 커야 합니다.")

    @property
    def is_buy(self):
        return self.side == SIDE_BUY

    @property
    def uses_amount(self):
        """True when this is a won/dollar-denominated order.

        Worth naming: amount orders and fractional quantities share a session
        restriction the risk gate has to apply (design 2.1).
        """
        return self.amount is not None

    def reference_price(self, market_price=None):
        """The price to value this signal at.

        A LIMIT signal is worth its own limit; a MARKET signal has no price of
        its own and has to borrow the last trade. Returns None when a MARKET
        signal is offered no market price - the caller decides whether that is
        fatal, because it is fatal for a notional check and harmless for a log
        line.
        """
        if self.limit_price is not None:
            return self.limit_price
        return market_price

    def notional(self, market_price=None):
        """Order value in the signal's own currency, or None if unknowable."""
        if self.amount is not None:
            return self.amount
        price = self.reference_price(market_price)
        if price is None:
            return None
        return self.quantity * price


@dataclass(frozen=True)
class MarketSession:
    """When one market is open, as reported by ``/market-calendar``.

    ``regular_close`` is kept as well as ``is_open`` because amount orders and
    fractional quantities stop being accepted an hour before the close while
    the market is still very much open (design 2.1).
    """

    country: str
    is_open: bool = False
    regular_close: datetime | None = None


@dataclass(frozen=True)
class DailyUsage:
    """How much of today's budget has already been spent.

    Read from the ``orders`` table by the caller and handed in, rather than
    queried here, so the risk gate stays a pure function of its inputs.
    """

    order_count: int = 0
    notional_krw: Decimal = ZERO


def session_date_of(history):
    """The latest completed session across ``{symbol: PriceHistory}``.

    The max, not any one symbol's: a single feed going quiet must not drag
    the whole run back a day. Returns None for an empty or unloaded history,
    leaving the fallback to the caller.
    """
    dates = [
        h.last_date for h in (history or {}).values() if getattr(h, "last_date", None)
    ]
    return max(dates) if dates else None


@dataclass(frozen=True)
class StrategyContext:
    """Everything a strategy - and then the risk gate - is allowed to see.

    One context serves both because they read the same world at the same
    instant; splitting it would only mean building two objects from one set of
    API responses. A strategy simply ignores the account fields.
    """

    now: datetime
    snapshot: object  # PortfolioSnapshot
    prices: dict = field(default_factory=dict)  # symbol -> Decimal

    # ---- account facts, for the risk gate -----------------------------
    buying_power: dict = field(default_factory=dict)  # currency -> Decimal
    sellable: dict = field(default_factory=dict)  # symbol -> Decimal
    price_limits: dict = field(default_factory=dict)  # symbol -> (low, high)
    sessions: dict = field(default_factory=dict)  # country -> MarketSession
    daily_usage: DailyUsage = field(default_factory=DailyUsage)
    kill_switch: bool = False

    #: ``{symbol: reason}`` for symbols whose *new buys* are paused - today
    #: the AI universe review writes these (src/universe_review.py). Read by
    #: the risk gate, never by a strategy: a strategy that could see the veto
    #: list would start reasoning about it, and the whole point is that this
    #: is a backstop applied after the strategy has had its say.
    blocked_symbols: dict = field(default_factory=dict)

    #: symbol -> PriceHistory, ending at (and including) the most recent
    #: completed session at or before ``now``. Populated by the context
    #: builder - live or backtest - never fetched here: an indicator a
    #: strategy computes from this is reproducible, one it fetches is not.
    #: Appended last, defaulted, so every existing construction site (live
    #: and test) keeps working with no history at all.
    history: dict = field(default_factory=dict)  # symbol -> PriceHistory

    #: Recent signals this strategy has already produced, oldest first, as the
    #: caller read them back from storage (or, in a backtest, from its own
    #: run so far). Exists so a rule that needs a cooldown - "don't buy the
    #: dip again for five days" - can read that state instead of a strategy
    #: keeping it, which purity forbids.
    recent: tuple = ()

    def bars(self, symbol):
        """The PriceHistory for ``symbol``, or None if none was supplied."""
        return self.history.get(symbol)

    @property
    def session_date(self):
        """The trading day this run is acting on, not the day it runs on.

        The two differ by design: the engine fires at 23:35 KST against the
        US session that closed that morning, so a run started at 23:35 and
        its retry at 00:05 are the same trading day under two wall-clock
        dates. Anything keyed on the wall clock therefore splits in half at
        KST midnight - which is 25 minutes after the job starts, and less
        than its 30-minute task timeout.

        Taken from the bars rather than from a calendar because the bars are
        what the strategies call ``today`` (``bucket_dca.evaluate``), and a
        second opinion here could disagree with the prices being traded on.
        """
        return session_date_of(self.history) or (
            self.now.date() if hasattr(self.now, "date") else self.now
        )

    def closes(self, symbol, n=None):
        """Adjusted closes for ``symbol``, oldest first, or () if unknown."""
        history = self.bars(symbol)
        return history.closes(n) if history else ()

    @property
    def positions(self):
        """``{symbol: Position}`` for everything currently held.

        Positions with no symbol - the static/manual entries - are skipped;
        there is nothing to trade against them.
        """
        held = {}
        for position in getattr(self.snapshot, "positions", []) or []:
            if position.symbol:
                held[position.symbol] = position
        return held

    @property
    def exchange_rate(self):
        return getattr(self.snapshot, "exchange_rate", ZERO) or ZERO

    @property
    def equity_krw(self):
        """Total portfolio value in KRW - the base for percentage limits."""
        return getattr(self.snapshot, "total_krw", ZERO) or ZERO

    def price(self, symbol):
        return self.prices.get(symbol)

    def position(self, symbol):
        return self.positions.get(symbol)

    def to_krw(self, value, currency):
        """Convert a native-currency amount to KRW at today's rate."""
        if value is None:
            return None
        if currency == "KRW":
            return value
        return value * self.exchange_rate


class Strategy(ABC):
    """Pure decision logic. Implement ``evaluate`` and nothing else.

    Deliberately given no client, no store and no config: a strategy that
    cannot reach the network cannot accidentally place an order, and cannot
    behave differently under test than it does in production.
    """

    #: Written to ``Signal.strategy`` and to every order this strategy places,
    #: so per-strategy performance can be attributed later.
    name = "unnamed"

    @classmethod
    def from_config(cls, trading_config=None):
        """Build this strategy from config.

        Config is read here, once, at construction - never inside
        ``evaluate``. Override this to pull a universe or parameters out of
        ``trading_config`` (see ``TradingConfig.universe`` and
        ``.strategy_params``); the default ignores config entirely, so a
        strategy with no parameters needs no override.
        """
        return cls()

    @abstractmethod
    def evaluate(self, ctx):
        """Return a list of :class:`Signal` for this context. May be empty."""
        raise NotImplementedError
