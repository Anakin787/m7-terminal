"""Replays a strategy through history using the real risk gate.

The loop, once per trading day:

1. Fill yesterday's approved intents at today's open.
2. Apply a contribution if one is due this month.
3. Mark to market and record the equity curve.
4. Build a context (no-lookahead: history is sliced to this day only).
5. ``strategy.evaluate(ctx)`` - the same call ``trade.py`` makes.
6. Every signal goes through the real :class:`~src.execution.risk.RiskGate`,
   logged whether approved or rejected, exactly like ``trade.py`` does.
7. Approved intents are queued for tomorrow's fill.

No mock anywhere in this list - see :mod:`src.backtest.sim` for why that
matters.
"""

from dataclasses import dataclass, field
from decimal import Decimal

from src.backtest.context import build_backtest_context
from src.backtest.fills import BPS, ContributionSchedule, FillModel
from src.backtest.metrics import BacktestResult, EquityPoint, summarize
from src.backtest.sim import SimPortfolio
from src.backtest.tax import RealisedGainLedger
from src.execution.risk import RiskGate, RiskLimits
from src.models import ZERO
from src.strategy.base import SIDE_SELL
from src.strategy.universe import BUCKET_SAFE


@dataclass(frozen=True)
class BacktestConfig:
    initial_krw: Decimal = Decimal("1000000")
    contribution: ContributionSchedule = field(default_factory=ContributionSchedule)
    fills: FillModel = field(default_factory=FillModel)
    limits: RiskLimits = field(default_factory=lambda: RiskLimits(strict=False))
    benchmark: str = "QQQ"
    #: Constant fallback used when no FX series is loaded. Backtests that care
    #: about FX precision should pass a real ``fx_history`` instead.
    fx_rate: Decimal = Decimal("1350")
    #: First day money is deposited and the strategy is allowed to act. Bars
    #: before it are *warm-up*: the strategy reads them, but nothing is bought
    #: and no contribution lands.
    #:
    #: Without this the first deposit arrives on the first bar in ``history``,
    #: while a momentum strategy cannot rank anything until it has
    #: ``required_bars`` (258 sessions ~ 1.03 years) behind it - so the run
    #: sits in 100% cash for a year while every benchmark curve is fully
    #: invested from day one. The comparison then measures which year the
    #: strategy happened to sit out, not the strategy: a 2022 start looked
    #: excellent purely by holding cash through 2022, and a 2020 start looked
    #: poor purely by holding cash through the recovery.
    trade_from: object = None
    #: Korean capital-gains tax on realised overseas-stock gains. Left on by
    #: default: a strategy that rotates realises gains by design, and every
    #: knob that changes turnover changes this bill, so comparing them with
    #: tax at zero would rank them on a cost the account cannot skip. Pass
    #: ``CapitalGainsTax(enabled=False)`` to see the pre-tax figure.
    tax: object = None
    #: Fixed-percent stop-loss, simulated as a resting order rather than a
    #: strategy signal - neither bucket_dca nor momentum_dca emit
    #: ``stop_loss_price`` today (see momentum_dca's docstring on why), so
    #: this exists to answer whether they should before wiring one in for
    #: real. Checked against each lot's live cost basis (recomputed on every
    #: add) and each day's *low*, before the strategy sees that day's
    #: context - the same sequencing a broker-side OCO bracket would have.
    #: SAFE-bucket holdings (SHY, GLD) are exempt: they exist to be held
    #: through the drawdowns this would cut them on. ``None`` disables it.
    stop_loss_pct: Decimal | None = None


class Backtester:
    def __init__(self, strategy, history, config=None, fx_history=None):
        self.strategy = strategy
        self.history = history  # {symbol: PriceHistory}, full range
        self.config = config or BacktestConfig()
        self.fx_history = fx_history  # optional PriceHistory of KRW=X

    def _fx_rate(self, day):
        if self.fx_history is not None:
            sliced = self.fx_history.as_of(day)
            last = sliced.last()
            if last is not None:
                return last.close
        return self.config.fx_rate

    def _is_safe_bucket(self, symbol):
        universe = getattr(self.strategy, "universe", None)
        if universe is None:
            return False
        instrument = universe.get(symbol)
        return instrument is not None and instrument.bucket == BUCKET_SAFE

    def run(self):
        benchmark = self.history.get(self.config.benchmark)
        if benchmark is None or not benchmark.bars:
            raise ValueError(f"벤치마크 {self.config.benchmark}의 시세 데이터가 없습니다.")

        dates = benchmark.dates
        if self.config.trade_from is not None:
            dates = [day for day in dates if day >= self.config.trade_from]
            if not dates:
                raise ValueError(
                    f"trade_from({self.config.trade_from}) 이후의 "
                    f"{self.config.benchmark} 봉이 없습니다."
                )
        universe_symbols = tuple(self.history.keys())

        sim = SimPortfolio(cash_usd=ZERO)
        ledger = RealisedGainLedger(self.config.tax)
        # The initial seed is contributed on day one like any other deposit,
        # so it flows through the same FX conversion and the same equity
        # curve accounting as every later contribution.
        gate = RiskGate(self.config.limits)

        pending = []  # [(intent, mode)] queued from yesterday, filled today
        recent_log = []  # dicts consumed by the strategy's `ctx.recent`
        decisions = []
        contributions = []  # (date, amount_krw)
        equity_curve = []
        contributed_this_month = None

        for i, day in enumerate(dates):
            sim.reset_daily(day)

            # 1. Fill yesterday's approved intents at today's open.
            day_bars = {
                symbol: self.history[symbol].as_of(day).last()
                for symbol in universe_symbols
                if symbol in self.history
            }
            still_pending = []
            for intent, mode in pending:
                bar = day_bars.get(intent.symbol)
                if bar is None or bar.date != day:
                    still_pending.append((intent, mode))  # no bar today, try again tomorrow
                    continue
                price = self.config.fills.fill_price(intent, bar)
                if price is None:
                    continue  # limit not touched - the order lapses
                if intent.amount is not None:
                    quantity = intent.amount / price
                else:
                    quantity = intent.quantity
                notional = quantity * price
                commission = self.config.fills.commission(notional)
                before = len(sim.trades)
                sim.apply_fill(day, intent.symbol, intent.side, quantity, price, commission, mode)
                for trade in sim.trades[before:]:
                    ledger.record(day, trade.pnl_usd, self._fx_rate(day))
            pending = still_pending

            # 1.5. Stop-loss, if configured: a resting order, checked against
            # today's low before anything else touches this position today.
            if self.config.stop_loss_pct is not None:
                for symbol in list(sim.lots.keys()):
                    if self._is_safe_bucket(symbol):
                        continue
                    bar = day_bars.get(symbol)
                    if bar is None or bar.date != day:
                        continue
                    lot = sim.lots[symbol]
                    stop_price = lot.avg_price * (1 - self.config.stop_loss_pct)
                    if bar.low > stop_price:
                        continue
                    fill_price = stop_price - stop_price * self.config.fills.slippage_bps / BPS
                    commission = self.config.fills.commission(lot.quantity * fill_price)
                    before = len(sim.trades)
                    sim.apply_fill(
                        day, symbol, SIDE_SELL, lot.quantity, fill_price, commission,
                        mode="stop_loss",
                    )
                    for trade in sim.trades[before:]:
                        ledger.record(day, trade.pnl_usd, self._fx_rate(day))

            # Last year's bill, settled once, on the first session of the new
            # year. Charged in cash, so it competes with everything else the
            # strategy wanted to do with that money - which is exactly how it
            # behaves in the account.
            owed_krw = ledger.due_on(day)
            if owed_krw > ZERO:
                sim.withdraw(owed_krw, self._fx_rate(day))

            # 2. Contribution.
            fx_rate = self._fx_rate(day)
            month_key = (day.year, day.month)
            if i == 0:
                sim.contribute(self.config.initial_krw, fx_rate)
                contributions.append((day, self.config.initial_krw))
                # The seed funds day one's month; the monthly schedule starts
                # from the next occurrence so it is never double-counted
                # against the initial deposit.
                contributed_this_month = month_key
            elif self.config.contribution.is_due(day, contributed_this_month == month_key):
                sim.contribute(self.config.contribution.amount_krw, fx_rate)
                contributions.append((day, self.config.contribution.amount_krw))
                contributed_this_month = month_key

            # 3. Mark to market.
            prices_today = {
                symbol: bar.price for symbol, bar in day_bars.items() if bar and bar.date == day
            }
            snapshot = sim.snapshot(prices_today, fx_rate)
            contributed_today = sum(
                (amount for d, amount in contributions if d == day), ZERO
            )
            equity_curve.append(
                EquityPoint(
                    date=day,
                    equity_krw=snapshot.total_krw,
                    equity_usd=snapshot.total_usd_equivalent,
                    contributed_today_krw=contributed_today,
                )
            )

            # 4-5. Context and strategy.
            # The live path passes ``store.recent_signals(limit=50)``; match
            # that window here so a cooldown rule that reads ``ctx.recent`` is
            # tested against the same bounded history it sees in production,
            # not an unbounded one that can never trip the truncation guard.
            ctx = build_backtest_context(
                day, sim, self.history, fx_rate, universe_symbols, recent=recent_log[-50:]
            )
            signals = self.strategy.evaluate(ctx) or []

            # 6. Real risk gate, every signal, approved or not. Batched for
            # the same reason the live path is: ctx carries the day's usage as
            # it stood before any of these signals, so evaluating them
            # independently lets one day's batch walk past a daily limit.
            for signal, decision in zip(signals, gate.evaluate_batch(signals, ctx)):
                decisions.append(decision)
                mode = (signal.meta or {}).get("mode")
                recent_log.append(
                    {
                        "ts": day.isoformat(),
                        "strategy": signal.strategy,
                        "symbol": signal.symbol,
                        "meta": dict(signal.meta or {}),
                        "outcome": "accepted" if decision.approved else "rejected",
                    }
                )
                if decision.approved:
                    notional_krw = decision.intent.notional_krw or ZERO
                    sim.record_intent(notional_krw)
                    pending.append((decision.intent, mode))

        result_metrics = summarize(equity_curve, sim.trades, decisions, contributions)
        result_metrics["tax_paid_krw"] = ledger.total_paid_krw
        return BacktestResult(
            equity_curve=tuple(equity_curve),
            trades=tuple(sim.trades),
            decisions=tuple(decisions),
            metrics=result_metrics,
        )
