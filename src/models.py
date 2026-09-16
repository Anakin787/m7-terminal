"""Unified domain model for holdings, independent of where they came from.

Toss returns every monetary field as a string. Parsing those into Decimal at
the boundary - rather than float - keeps won-level sums exact and removes the
rounding drift the previous yfinance-based implementation carried.
"""

from dataclasses import dataclass, field, replace
from decimal import Decimal, InvalidOperation

ZERO = Decimal("0")

SOURCE_TOSS = "toss"
SOURCE_MANUAL = "manual"


def to_decimal(value, default=None):
    """Parse an API value into Decimal, returning ``default`` when unusable."""
    if value is None or value == "":
        return default
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return default


def display_name(item, symbol=None):
    """Pick the most informative name Toss offers for a security.

    For US listings Toss has no Korean name, so it echoes the ticker back in
    ``name`` and puts the real one in ``englishName``. Taking ``name`` at face
    value leaves the ticker standing alone everywhere - and an unfamiliar
    ticker is exactly what the AI analyst then guesses at. IONX came back as
    "IONX" and got read as IonQ common stock rather than the 2x leveraged ETF
    it is. Korean names still win when there is one.
    """
    item = item or {}
    symbol = symbol or item.get("symbol")
    name = item.get("name")
    if not name or name == symbol:
        name = item.get("englishName") or name
    return name or symbol or ""


def dig(mapping, *keys, default=None):
    """Walk nested dicts safely - Toss nests amounts several levels deep."""
    current = mapping
    for key in keys:
        if not isinstance(current, dict):
            return default
        current = current.get(key)
        if current is None:
            return default
    return current


@dataclass(frozen=True)
class Position:
    """One holding, normalised across the Toss account and manual entries."""

    symbol: str | None
    name: str
    market_country: str  # "KR" | "US" | "" for static assets
    currency: str  # "KRW" | "USD"
    quantity: Decimal
    last_price: Decimal
    avg_purchase_price: Decimal
    source: str  # SOURCE_TOSS | SOURCE_MANUAL

    # Present for Toss positions, where the server already did the maths
    # including fees and tax. Manual positions derive what they can.
    market_value: Decimal | None = None
    purchase_amount: Decimal | None = None
    profit_loss: Decimal | None = None
    profit_loss_after_cost: Decimal | None = None
    profit_rate: Decimal | None = None
    profit_rate_after_cost: Decimal | None = None
    daily_profit_loss: Decimal | None = None
    daily_profit_rate: Decimal | None = None

    #: Only ever set from config. Toss does not report the exchange rate that
    #: applied when a foreign position was bought.
    avg_exchange_rate: Decimal | None = None

    #: What kind of security this is, straight from the Toss master record.
    #: "ETF", "STOCK", ... and the daily reset multiple for leveraged and
    #: inverse products (negative means inverse).
    security_type: str | None = None
    leverage_factor: Decimal | None = None

    #: The single company this product actually tracks (TSLL -> "TSLA"),
    #: only ever set from config.yaml's portfolio.manual[].underlying today.
    #: Feeds the report's per-holding news search, which a leveraged product's
    #: own listed name searches poorly - see ManualHolding.underlying.
    underlying: str | None = None

    @property
    def is_foreign(self):
        return self.currency != "KRW"

    @property
    def instrument(self):
        """A short description of what this security actually is.

        The AI analyst reads tickers it may not know and will otherwise guess
        the instrument from the name - reading a 2x ETF as the underlying
        company's shares, and giving advice that suits shares rather than a
        product that decays when the market chops sideways.
        """
        parts = []
        factor = self.leverage_factor
        if factor is not None and factor != 1:
            if factor < 0:
                parts.append(f"{abs(factor):g}x inverse (daily reset)")
            else:
                parts.append(f"{factor:g}x leveraged (daily reset)")
        if self.security_type:
            parts.append(self.security_type)
        return " ".join(parts)

    @property
    def evaluation(self):
        """Market value in the position's own currency."""
        if self.market_value is not None:
            return self.market_value
        return self.quantity * self.last_price

    @property
    def cost_basis(self):
        """Purchase amount in the position's own currency."""
        if self.purchase_amount is not None:
            return self.purchase_amount
        return self.quantity * self.avg_purchase_price

    def with_quantity(self, quantity):
        """This holding as if it were only ``quantity`` shares.

        Every absolute figure is scaled by the same ratio; the rates are left
        alone, being ratios already. ``market_value`` in particular has to be
        scaled rather than dropped: ``evaluation`` prefers it over
        quantity times price, so a copy with a smaller quantity and the
        original market value would report the full holding's worth under a
        fraction of its shares.

        Returns None when nothing is left, so a caller can drop the position
        entirely rather than carry a zero-share one.
        """
        quantity = to_decimal(quantity, default=ZERO) or ZERO
        if quantity <= ZERO:
            return None
        if self.quantity <= ZERO or quantity >= self.quantity:
            return self

        ratio = quantity / self.quantity
        scaled = {
            field: (value * ratio if value is not None else None)
            for field, value in (
                ("market_value", self.market_value),
                ("purchase_amount", self.purchase_amount),
                ("profit_loss", self.profit_loss),
                ("profit_loss_after_cost", self.profit_loss_after_cost),
                ("daily_profit_loss", self.daily_profit_loss),
            )
        }
        return replace(self, quantity=quantity, **scaled)


@dataclass
class PortfolioSnapshot:
    """Everything the report and the dashboard need for one point in time."""

    positions: list = field(default_factory=list)
    exchange_rate: Decimal = ZERO

    # Totals converted to KRW
    total_krw: Decimal = ZERO
    purchase_krw: Decimal = ZERO
    profit_krw: Decimal = ZERO
    profit_rate: Decimal = ZERO
    profit_after_cost_krw: Decimal | None = None
    profit_rate_after_cost: Decimal | None = None
    daily_profit_krw: Decimal = ZERO
    daily_profit_rate: Decimal = ZERO

    # Per-currency subtotals, before conversion
    by_currency: dict = field(default_factory=dict)

    #: True when at least one foreign position has no avg_exchange_rate, so
    #: the return excludes FX gain/loss. Surfaced as a badge so the number is
    #: not read as something it is not.
    has_unconverted_fx: bool = False

    warnings: list = field(default_factory=list)
    buying_power: dict = field(default_factory=dict)

    @property
    def total_usd_equivalent(self):
        if not self.exchange_rate:
            return None
        return self.total_krw / self.exchange_rate

    def evaluation_krw(self, position):
        """Market value converted at today's rate."""
        rate = self.exchange_rate if position.is_foreign else Decimal("1")
        return position.evaluation * rate

    def cost_krw(self, position):
        """Purchase amount converted at the rate that applied when bought.

        Falls back to today's rate when the position has no
        ``avg_exchange_rate`` - matching the aggregator, so the per-row costs
        add up to ``purchase_krw``.
        """
        if not position.is_foreign:
            return position.cost_basis
        rate = position.avg_exchange_rate or self.exchange_rate
        return position.cost_basis * rate

    def fx_pnl_krw(self, position):
        """The slice of P&L caused by the won moving, isolated from price.

        Splitting ``evaluation_krw - cost_krw`` into a price effect and an FX
        effect: valuing the price change at today's rate and the FX change
        against the unchanged cost basis accounts for the total exactly,
        with no residual -

            (evaluation_native - cost_native) * today_rate      # price effect
          + cost_native * (today_rate - avg_rate)                # fx effect
          = evaluation_native * today_rate - cost_native * avg_rate
          = total P&L in KRW

        None for KRW positions and for foreign ones with no
        ``avg_exchange_rate`` - same condition as ``has_unconverted_fx``,
        since ``cost_krw`` already falls back to today's rate there and the
        FX effect would be a meaningless zero.
        """
        if not position.is_foreign or not position.avg_exchange_rate:
            return None
        return position.cost_basis * (self.exchange_rate - position.avg_exchange_rate)

    @property
    def total_fx_pnl_krw(self):
        """Sum of ``fx_pnl_krw`` across positions that report one, or None."""
        values = [self.fx_pnl_krw(p) for p in self.positions]
        values = [v for v in values if v is not None]
        if not values:
            return None
        return sum(values, ZERO)

    def allocation(self, by="market"):
        """Group market value in KRW for the dashboard's donut chart."""
        buckets = {}
        for position in self.positions:
            if by == "currency":
                key = position.currency
            else:
                key = position.market_country or "OTHER"
            rate = self.exchange_rate if position.is_foreign else Decimal("1")
            buckets[key] = buckets.get(key, ZERO) + position.evaluation * rate
        return buckets
