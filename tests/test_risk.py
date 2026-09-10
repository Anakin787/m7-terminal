"""The risk gate. Every rule gets a test that shows it firing."""

from datetime import datetime
from decimal import Decimal

from src.execution.risk import (
    RiskGate,
    RiskLimits,
    engage_kill_switch,
    kill_switch_active,
    kill_switch_state,
    release_kill_switch,
)
from src.models import SOURCE_TOSS, PortfolioSnapshot, Position
from src.strategy.base import (
    ORDER_MARKET,
    SIDE_BUY,
    SIDE_SELL,
    DailyUsage,
    MarketSession,
    Signal,
    StrategyContext,
)

RATE = Decimal("1400")
NOW = datetime(2026, 8, 26, 10, 0)

#: Limits wide enough that budget and concentration never fire, for the tests
#: that are about some other rule. A 2,000 USD order is 28% of the fixture
#: portfolio and would otherwise trip the weight cap first.
_ROOMY = RiskLimits(
    max_daily_notional_krw=Decimal("999999999"),
    max_position_weight=Decimal("1"),
)

KR_OPEN = MarketSession("KR", is_open=True, regular_close=datetime(2026, 8, 26, 15, 30))
US_OPEN = MarketSession("US", is_open=True, regular_close=datetime(2026, 8, 26, 23, 0))


def position(**overrides):
    base = dict(
        symbol="005930",
        name="삼성전자",
        market_country="KR",
        currency="KRW",
        quantity=Decimal("10"),
        last_price=Decimal("70000"),
        avg_purchase_price=Decimal("65000"),
        source=SOURCE_TOSS,
    )
    base.update(overrides)
    return Position(**base)


def buy(**overrides):
    base = dict(
        strategy="test",
        symbol="005930",
        side=SIDE_BUY,
        reason="테스트",
        quantity=Decimal("10"),
        limit_price=Decimal("70000"),
        currency="KRW",
    )
    base.update(overrides)
    return Signal(**base)


def context(**overrides):
    """A context where a default buy() passes every rule."""
    snapshot = PortfolioSnapshot(
        positions=[position()],
        exchange_rate=RATE,
        total_krw=Decimal("10000000"),
    )
    base = dict(
        now=NOW,
        snapshot=snapshot,
        prices={"005930": Decimal("70000"), "AAPL": Decimal("200")},
        buying_power={"KRW": Decimal("5000000"), "USD": Decimal("10000")},
        sellable={"005930": Decimal("10")},
        price_limits={
            "005930": (Decimal("50000"), Decimal("90000")),
            "AAPL": (Decimal("150"), Decimal("250")),
        },
        sessions={"KR": KR_OPEN, "US": US_OPEN},
        daily_usage=DailyUsage(),
    )
    base.update(overrides)
    return StrategyContext(**base)


def evaluate(signal=None, ctx=None, limits=None):
    return RiskGate(limits).evaluate(signal or buy(), ctx or context())


def rule_of(decision):
    return decision.rejection.rule if decision.rejection else None


# ------------------------------------------------------------- happy path


def test_a_clean_signal_becomes_an_order_intent():
    decision = evaluate()
    assert decision.approved
    intent = decision.intent
    assert (intent.symbol, intent.side) == ("005930", SIDE_BUY)
    assert intent.notional_krw == Decimal("700000")
    assert intent.confirm_high_value is False
    # The executor assigns this in step [8]; the gate leaves it open.
    assert intent.client_order_id is None


def test_intent_carries_the_strategy_name_for_attribution():
    assert evaluate().intent.strategy == "test"


def test_market_intent_drops_the_limit_price():
    decision = evaluate(
        buy(order_type=ORDER_MARKET, limit_price=None), context()
    )
    assert decision.approved
    assert decision.intent.limit_price is None
    assert decision.intent.notional_krw == Decimal("700000")


# ------------------------------------------------------------ kill switch


def test_a_vetoed_symbol_cannot_be_bought():
    decision = evaluate(buy(), context(blocked_symbols={"005930": "상장폐지 절차"}))
    assert rule_of(decision) == "symbol-vetoed"
    assert "상장폐지 절차" in decision.rejection.detail


def test_a_veto_never_blocks_a_sell():
    # A veto is a reason not to add exposure, never a reason to dump the
    # position - selling stays the strategy's decision.
    decision = evaluate(
        buy(side=SIDE_SELL, quantity=Decimal("10")),
        context(blocked_symbols={"005930": "거래정지"}),
    )
    assert decision.approved


def test_a_veto_on_another_symbol_is_ignored():
    decision = evaluate(buy(), context(blocked_symbols={"AAPL": "합병"}))
    assert decision.approved


def test_the_kill_switch_still_outranks_a_veto():
    decision = evaluate(
        buy(), context(kill_switch=True, blocked_symbols={"005930": "거래정지"})
    )
    assert rule_of(decision) == "kill-switch"


def test_kill_switch_stops_everything():
    decision = evaluate(buy(), context(kill_switch=True))
    assert rule_of(decision) == "kill-switch"


def test_kill_switch_beats_an_otherwise_perfect_signal():
    # Ordering matters: the switch is checked before anything else, so it
    # still fires when every other rule would have passed.
    decision = evaluate(buy(quantity=Decimal("1")), context(kill_switch=True))
    assert rule_of(decision) == "kill-switch"


def test_kill_switch_file_detection(tmp_path):
    path = tmp_path / "KILL_SWITCH"
    assert kill_switch_active(str(path)) is False
    path.write_text("")
    assert kill_switch_active(str(path)) is True


# ---------------------------------------------------------- daily budget


def test_daily_order_count_limit():
    ctx = context(daily_usage=DailyUsage(order_count=10))
    assert rule_of(evaluate(buy(), ctx)) == "daily-order-limit"


def test_daily_notional_limit_counts_what_was_already_spent():
    ctx = context(daily_usage=DailyUsage(notional_krw=Decimal("4500000")))
    limits = RiskLimits(max_daily_notional_krw=Decimal("5000000"))
    # 4,500,000 spent + 700,000 proposed = 5,200,000 > 5,000,000
    assert rule_of(evaluate(buy(), ctx, limits)) == "daily-notional-limit"


def test_daily_notional_limit_allows_an_exact_fit():
    ctx = context(daily_usage=DailyUsage(notional_krw=Decimal("4300000")))
    limits = RiskLimits(max_daily_notional_krw=Decimal("5000000"))
    assert evaluate(buy(), ctx, limits).approved


# ------------------------------------------------------ position weight


def test_position_weight_counts_the_holding_the_order_would_create():
    # Already holding 700,000 of a 10,000,000 portfolio; buying 1,500,000 more
    # lands at 22%, over the 20% cap - even though the order alone is only 15%.
    signal = buy(quantity=Decimal("21"), limit_price=Decimal("71500"))
    limits = RiskLimits(max_daily_notional_krw=Decimal("99999999"))
    assert rule_of(evaluate(signal, context(), limits)) == "position-weight-limit"


def test_selling_is_never_blocked_by_concentration():
    signal = buy(side=SIDE_SELL, quantity=Decimal("10"))
    limits = RiskLimits(max_position_weight=Decimal("0.001"))
    assert evaluate(signal, context(), limits).approved


def test_per_symbol_override_permits_a_concentration_the_default_forbids():
    # Same 22% buy as above, but this symbol has a config exception.
    signal = buy(quantity=Decimal("21"), limit_price=Decimal("71500"))
    limits = RiskLimits(
        max_daily_notional_krw=Decimal("99999999"),
        max_position_weight_overrides={"005930": Decimal("0.60")},
    )
    assert evaluate(signal, context(), limits).approved


def test_override_does_not_relax_the_limit_for_a_different_symbol():
    signal = buy(quantity=Decimal("21"), limit_price=Decimal("71500"))
    limits = RiskLimits(
        max_daily_notional_krw=Decimal("99999999"),
        max_position_weight_overrides={"AAPL": Decimal("0.60")},
    )
    assert rule_of(evaluate(signal, context(), limits)) == "position-weight-limit"


def test_small_equity_is_exempt_from_the_weight_check():
    # A first-ever buy is, by definition, ~100% of the account - exactly the
    # case the small-equity exemption exists to let through.
    snapshot = PortfolioSnapshot(
        positions=[], exchange_rate=RATE, total_krw=Decimal("700000")
    )
    ctx = context(snapshot=snapshot)
    signal = buy(quantity=Decimal("10"))
    limits = RiskLimits(
        max_daily_notional_krw=Decimal("99999999"),
        weight_check_min_equity_krw=Decimal("3000000"),
    )
    decision = evaluate(signal, ctx, limits)
    assert rule_of(decision) != "position-weight-limit"


def test_weight_check_reapplies_above_the_small_equity_threshold():
    signal = buy(quantity=Decimal("21"), limit_price=Decimal("71500"))
    limits = RiskLimits(
        max_daily_notional_krw=Decimal("99999999"),
        weight_check_min_equity_krw=Decimal("1"),
    )
    assert rule_of(evaluate(signal, context(), limits)) == "position-weight-limit"


# ------------------------------------------------------------- sessions


def test_closed_market_is_rejected():
    ctx = context(sessions={"KR": MarketSession("KR", is_open=False)})
    assert rule_of(evaluate(buy(), ctx)) == "order-hours-closed"


def test_amount_order_is_refused_in_the_last_hour():
    ctx = context(now=datetime(2026, 8, 26, 14, 45))
    signal = buy(quantity=None, amount=Decimal("500000"))
    assert rule_of(evaluate(signal, ctx)) == "amount-order-outside-regular-hours"


def test_amount_order_is_fine_earlier_in_the_session():
    signal = buy(quantity=None, amount=Decimal("500000"))
    assert evaluate(signal, context()).approved


def test_share_orders_are_unaffected_by_the_early_cutoff():
    ctx = context(now=datetime(2026, 8, 26, 15, 20))
    assert evaluate(buy(), ctx).approved


# --------------------------------------------------- fractional quantity


def test_fractional_quantity_allowed_only_for_us_market_sell():
    ctx = context(sellable={"AAPL": Decimal("10")})
    ok = buy(
        symbol="AAPL",
        side=SIDE_SELL,
        currency="USD",
        order_type=ORDER_MARKET,
        limit_price=None,
        quantity=Decimal("1.5"),
    )
    # 23:00 close, so 10:00 is well clear of the one-hour cutoff.
    assert evaluate(ok, ctx).approved


def test_fractional_quantity_rejected_for_a_korean_buy():
    signal = buy(quantity=Decimal("1.5"))
    assert rule_of(evaluate(signal, context())) == "fractional-quantity-not-allowed"


def test_fractional_quantity_rejected_for_a_us_limit_sell():
    ctx = context(sellable={"AAPL": Decimal("10")})
    signal = buy(
        symbol="AAPL",
        side=SIDE_SELL,
        currency="USD",
        quantity=Decimal("1.5"),
        limit_price=Decimal("200"),
    )
    assert rule_of(evaluate(signal, ctx)) == "fractional-quantity-not-allowed"


# ----------------------------------------------------------- price band


def test_limit_above_the_upper_band_is_rejected():
    signal = buy(limit_price=Decimal("95000"))
    assert rule_of(evaluate(signal, context())) == "price-out-of-range"


def test_limit_below_the_lower_band_is_rejected():
    signal = buy(limit_price=Decimal("40000"))
    assert rule_of(evaluate(signal, context())) == "price-out-of-range"


def test_market_orders_skip_the_band_check():
    # A market order has no price of its own, so the band cannot apply - and
    # missing band data must not block it even in strict mode.
    ctx = context(price_limits={})
    signal = buy(order_type=ORDER_MARKET, limit_price=None)
    assert evaluate(signal, ctx).approved


# --------------------------------------------------------------- balance


def test_insufficient_buying_power():
    ctx = context(buying_power={"KRW": Decimal("100000")})
    assert rule_of(evaluate(buy(), ctx)) == "insufficient-buying-power"


def test_insufficient_sellable_quantity():
    signal = buy(side=SIDE_SELL, quantity=Decimal("50"))
    assert rule_of(evaluate(signal, context())) == "insufficient-sellable-quantity"


def test_foreign_buying_power_is_checked_in_its_own_currency():
    signal = buy(
        symbol="AAPL", currency="USD", quantity=Decimal("10"), limit_price=Decimal("200")
    )
    ctx = context(buying_power={"USD": Decimal("500")})
    assert rule_of(evaluate(signal, ctx, _ROOMY)) == "insufficient-buying-power"


def test_foreign_notional_is_converted_for_the_krw_budget():
    signal = buy(
        symbol="AAPL", currency="USD", quantity=Decimal("10"), limit_price=Decimal("200")
    )
    decision = evaluate(signal, context(), _ROOMY)
    assert decision.approved
    assert decision.intent.notional_krw == Decimal("2800000")  # 2,000 USD * 1,400


# ------------------------------------------------------------ high value


def test_high_value_order_needs_explicit_permission():
    signal = buy(quantity=Decimal("2000"), limit_price=Decimal("70000"))
    limits = RiskLimits(
        max_daily_notional_krw=Decimal("999999999"),
        max_position_weight=Decimal("100"),
    )
    ctx = context(buying_power={"KRW": Decimal("999999999")})
    assert rule_of(evaluate(signal, ctx, limits)) == "high-value-not-permitted"


def test_permitted_high_value_order_sets_the_confirm_flag():
    signal = buy(quantity=Decimal("2000"), limit_price=Decimal("70000"))
    limits = RiskLimits(
        max_daily_notional_krw=Decimal("999999999"),
        max_position_weight=Decimal("100"),
        allow_high_value=True,
    )
    ctx = context(buying_power={"KRW": Decimal("999999999")})
    decision = evaluate(signal, ctx, limits)
    assert decision.approved
    assert decision.intent.confirm_high_value is True


# ----------------------------------------------------- missing reference data


def test_strict_mode_rejects_what_it_cannot_verify():
    cases = {
        "buying-power-unknown": context(buying_power={}),
        "sellable-quantity-unknown": None,  # handled below
        "market-session-unknown": context(sessions={}),
        "price-limits-unknown": context(price_limits={}),
    }
    assert rule_of(evaluate(buy(), cases["buying-power-unknown"])) == "buying-power-unknown"
    assert rule_of(evaluate(buy(), cases["market-session-unknown"])) == "market-session-unknown"
    assert rule_of(evaluate(buy(), cases["price-limits-unknown"])) == "price-limits-unknown"

    sell = buy(side=SIDE_SELL)
    assert rule_of(evaluate(sell, context(sellable={}))) == "sellable-quantity-unknown"


def test_strict_mode_rejects_a_market_order_with_no_quote():
    signal = buy(order_type=ORDER_MARKET, limit_price=None)
    ctx = context(prices={})
    assert rule_of(evaluate(signal, ctx)) == "price-unknown"


def test_lenient_mode_checks_only_what_it_can():
    lenient = RiskLimits(strict=False)
    bare = context(
        prices={}, buying_power={}, sellable={}, price_limits={}, sessions={}
    )
    assert evaluate(buy(), bare, lenient).approved


def test_lenient_mode_still_enforces_the_kill_switch():
    lenient = RiskLimits(strict=False)
    bare = context(
        prices={},
        buying_power={},
        sellable={},
        price_limits={},
        sessions={},
        kill_switch=True,
    )
    assert rule_of(evaluate(buy(), bare, lenient)) == "kill-switch"


def test_lenient_mode_still_enforces_the_daily_limits():
    lenient = RiskLimits(strict=False)
    bare = context(
        prices={},
        buying_power={},
        sellable={},
        price_limits={},
        sessions={},
        daily_usage=DailyUsage(order_count=99),
    )
    assert rule_of(evaluate(buy(), bare, lenient)) == "daily-order-limit"


def test_engaging_the_kill_switch_records_why_in_the_file(tmp_path):
    path = str(tmp_path / "KILL_SWITCH")

    state = engage_kill_switch(path, reason="  브로커 오류  확인 중 ", actor="dashboard")

    assert kill_switch_active(path) is True
    assert state["active"] is True
    assert state["actor"] == "dashboard"
    # Whitespace collapsed so a pasted reason cannot break the line format.
    assert state["reason"] == "브로커 오류 확인 중"
    assert state["engaged_at_source"] == "file"


def test_a_hand_made_kill_switch_file_is_a_valid_stop(tmp_path):
    """``touch KILL_SWITCH`` must work: that is the point of using a file."""
    path = tmp_path / "KILL_SWITCH"
    path.write_text("")

    state = kill_switch_state(str(path))

    assert state["active"] is True
    assert state["reason"] is None
    # No header to read, so the time is the file's - and says so rather than
    # presenting an mtime as if it were a recorded decision.
    assert state["engaged_at_source"] == "mtime"
    assert state["engaged_at"] is not None


def test_releasing_reports_whether_it_had_been_engaged(tmp_path):
    path = str(tmp_path / "KILL_SWITCH")
    engage_kill_switch(path)

    assert release_kill_switch(path) is True
    assert release_kill_switch(path) is False
    assert kill_switch_state(path)["active"] is False


# ------------------------------------------------------------ batch limits
#
# One run hands the gate every signal a strategy produced, but the context was
# read before any of them existed. Each of these fixes a way that used to let
# a batch walk straight past a limit its members individually respected.
# 2026-09-09 is the run that made this concrete: 8 approvals against a stored
# order_count of 0.


def rules_of(decisions):
    return [rule_of(d) for d in decisions]


def test_a_batch_stops_at_the_daily_order_count():
    limits = RiskLimits(max_orders_per_day=3, max_position_weight=Decimal("1"))
    signals = [buy(quantity=Decimal("1")) for _ in range(5)]

    decisions = RiskGate(limits).evaluate_batch(signals, context())

    assert rules_of(decisions) == [None, None, None, "daily-order-limit", "daily-order-limit"]


def test_a_batch_counts_from_what_the_day_already_spent():
    """The stored usage and the run's own approvals are the same budget."""
    ctx = context(daily_usage=DailyUsage(order_count=2))
    limits = RiskLimits(max_orders_per_day=3, max_position_weight=Decimal("1"))

    decisions = RiskGate(limits).evaluate_batch([buy(quantity=Decimal("1"))] * 2, ctx)

    assert rules_of(decisions) == [None, "daily-order-limit"]


def test_a_batch_stops_at_the_daily_notional():
    # 700,000 per order; the third would make 2,100,000.
    limits = RiskLimits(
        max_daily_notional_krw=Decimal("2000000"), max_position_weight=Decimal("1")
    )

    decisions = RiskGate(limits).evaluate_batch([buy()] * 3, context())

    assert rules_of(decisions) == [None, None, "daily-notional-limit"]
    assert "2,100,000" in decisions[2].rejection.detail


def test_a_batch_cannot_spend_the_same_buying_power_twice():
    signal = buy(
        symbol="AAPL", currency="USD", quantity=Decimal("10"), limit_price=Decimal("200")
    )
    ctx = context(buying_power={"USD": Decimal("2500")})

    decisions = RiskGate(_ROOMY).evaluate_batch([signal] * 2, ctx)

    # 2,000 USD each: the first fits in 2,500, the second has 500 left.
    assert rules_of(decisions) == [None, "insufficient-buying-power"]
    assert "주문가능 500" in decisions[1].rejection.detail


def test_a_market_buy_commits_the_price_the_gate_used():
    """A MARKET order has no limit price, so its spend comes from the quote."""
    signal = buy(
        symbol="AAPL",
        currency="USD",
        order_type=ORDER_MARKET,
        quantity=Decimal("10"),
        limit_price=None,
    )
    ctx = context(buying_power={"USD": Decimal("2500")})

    decisions = RiskGate(_ROOMY).evaluate_batch([signal] * 2, ctx)

    # 10 x 200 = 2,000 quoted, not 0 - which is what an unpriced commitment
    # would have committed, letting the second order through.
    assert rules_of(decisions) == [None, "insufficient-buying-power"]


def test_a_batch_cannot_sell_the_same_shares_twice():
    signal = buy(side=SIDE_SELL, quantity=Decimal("6"))

    decisions = RiskGate(_ROOMY).evaluate_batch([signal] * 2, context())

    # 10 sellable: 6 goes, 6 more does not.
    assert rules_of(decisions) == [None, "insufficient-sellable-quantity"]
    assert "매도가능 4주" in decisions[1].rejection.detail


def test_a_batch_concentrates_into_the_weight_cap():
    # Already holding 700,000 of a 10,000,000 portfolio, and each order adds
    # another 700,000. One order lands at 14%, inside the 20% cap; two land at
    # 21%. Evaluated independently the second is 14% too, and goes through.
    limits = RiskLimits(max_daily_notional_krw=Decimal("99999999"))

    decisions = RiskGate(limits).evaluate_batch([buy()] * 2, context())

    assert rules_of(decisions) == [None, "position-weight-limit"]
    assert "21.0%" in decisions[1].rejection.detail


def test_a_rejected_signal_commits_nothing():
    """Only orders consume budget, the same rule ``daily_usage`` follows."""
    limits = RiskLimits(
        max_daily_notional_krw=Decimal("2000000"), max_position_weight=Decimal("1")
    )
    # The middle signal is rejected for an unrelated reason (no quote), and
    # must not count toward the two orders the notional cap allows.
    unpriced = buy(symbol="UNKNOWN", order_type=ORDER_MARKET, limit_price=None)

    decisions = RiskGate(limits).evaluate_batch([buy(), unpriced, buy(), buy()], context())

    assert rules_of(decisions) == [None, "price-unknown", None, "daily-notional-limit"]


def test_a_lone_evaluate_is_unchanged_by_the_batch_machinery():
    """The single-signal path still judges against the context alone."""
    decision = RiskGate().evaluate(buy(), context())

    assert decision.approved
    assert decision.intent.notional_krw == Decimal("700000")
    assert decision.intent.notional == Decimal("700000")
