"""Merge holding sources into a single KRW-denominated snapshot.

Toss reports totals split by currency and offers no combined KRW figure, so
the conversion happens here. The rule carried over from v1:

    evaluation_KRW = sum(KRW evaluation) + sum(USD evaluation) * current_rate
    cost_KRW       = sum(KRW cost)       + sum(USD cost) * (avg_exchange_rate
                                                            or current_rate)

Using the purchase-time rate for the cost side is what makes FX gain/loss
show up in the return. Toss does not report that rate, so it only applies to
positions where the config supplies ``avg_exchange_rate``.
"""

from decimal import Decimal

from src.models import ZERO, PortfolioSnapshot, Position, to_decimal

ONE = Decimal("1")


class HoldingsAggregator:
    def __init__(self, sources, market_api):
        self.sources = list(sources)
        self.market_api = market_api

    def build(self):
        positions = []
        for source in self.sources:
            positions.extend(source.fetch())

        positions = _merge_duplicates(positions)
        rate = self._exchange_rate()
        return _summarise(positions, rate)

    def _exchange_rate(self):
        result = self.market_api.exchange_rate("USD", "KRW") or {}
        rate = to_decimal(result.get("rate"))
        if rate is None or rate <= 0:
            raise ValueError(
                "USD/KRW 환율을 조회하지 못했습니다. 원화 환산을 진행할 수 없습니다."
            )
        return rate


def _merge_duplicates(positions):
    """Combine positions in the same symbol held through different sources.

    Holding the same stock at Toss and at another broker should read as one
    line. Quantities add; the average purchase price becomes the
    quantity-weighted average. Server-computed fields are dropped on merge,
    because Toss's numbers describe only its own share of the position.
    """
    merged = {}
    order = []

    for position in positions:
        key = (position.symbol, position.currency) if position.symbol else id(position)
        if key not in merged:
            merged[key] = position
            order.append(key)
            continue

        existing = merged[key]
        total_qty = existing.quantity + position.quantity
        if total_qty == 0:
            continue

        weighted_avg = (
            existing.quantity * existing.avg_purchase_price
            + position.quantity * position.avg_purchase_price
        ) / total_qty

        merged[key] = Position(
            symbol=existing.symbol,
            name=existing.name or position.name,
            market_country=existing.market_country or position.market_country,
            currency=existing.currency,
            quantity=total_qty,
            last_price=position.last_price or existing.last_price,
            avg_purchase_price=weighted_avg,
            source=f"{existing.source}+{position.source}",
            daily_profit_loss=_add_optional(
                existing.daily_profit_loss, position.daily_profit_loss
            ),
            avg_exchange_rate=existing.avg_exchange_rate or position.avg_exchange_rate,
        )

    return [merged[key] for key in order]


def _add_optional(left, right):
    if left is None:
        return right
    if right is None:
        return left
    return left + right


def _summarise(positions, rate):
    snapshot = PortfolioSnapshot(positions=positions, exchange_rate=rate)

    total_krw = ZERO
    purchase_krw = ZERO
    after_cost_krw = ZERO
    daily_krw = ZERO
    #: After-cost figures are summed only when *every* position reports one.
    #: Toss gives ``amountAfterCost`` for some holdings and not others (the
    #: shares bought through the API have it; older holdings did not), and a
    #: partial sum divided by the whole portfolio's cost read -0.00% for a
    #: portfolio down 28%.
    after_cost_complete = bool(positions)
    by_currency = {}

    for position in positions:
        currency = position.currency
        eval_native = position.evaluation
        cost_native = position.cost_basis

        bucket = by_currency.setdefault(
            currency, {"evaluation": ZERO, "purchase": ZERO}
        )
        bucket["evaluation"] += eval_native
        bucket["purchase"] += cost_native

        eval_rate = rate if position.is_foreign else ONE
        # The purchase-time rate only differs for foreign positions that
        # declared one; everything else converts at today's rate.
        cost_rate = eval_rate
        if position.is_foreign and position.avg_exchange_rate:
            cost_rate = position.avg_exchange_rate
        elif position.is_foreign:
            snapshot.has_unconverted_fx = True

        total_krw += eval_native * eval_rate
        purchase_krw += cost_native * cost_rate

        if position.profit_loss_after_cost is None:
            after_cost_complete = False
        else:
            after_cost_krw += position.profit_loss_after_cost * eval_rate

        if position.daily_profit_loss is not None:
            daily_krw += position.daily_profit_loss * eval_rate

    snapshot.total_krw = total_krw
    snapshot.purchase_krw = purchase_krw
    snapshot.profit_krw = total_krw - purchase_krw
    snapshot.profit_rate = _rate(snapshot.profit_krw, purchase_krw)
    snapshot.daily_profit_krw = daily_krw
    previous = total_krw - daily_krw
    snapshot.daily_profit_rate = _rate(daily_krw, previous)
    snapshot.by_currency = by_currency

    if after_cost_complete:
        snapshot.profit_after_cost_krw = after_cost_krw
        snapshot.profit_rate_after_cost = _rate(after_cost_krw, purchase_krw)

    return snapshot


def _rate(numerator, denominator):
    """Return a ratio (0.1179 = +11.79%), or zero when there is no base."""
    if not denominator:
        return ZERO
    return numerator / denominator
