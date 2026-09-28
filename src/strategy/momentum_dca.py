"""Momentum-weighted, concentrated dollar-cost averaging.

The brief this implements: a small account, ~50-100만원 arriving monthly, that
wants to be aggressive without being reckless. "Aggressive" here means
concentration - new cash goes to the one or two names with the strongest
recent trend rather than being split evenly - not leverage beyond 2x and not a
wide stop-loss grid (see :mod:`src.strategy.universe` for the leverage policy).

No per-position stop-loss. This strategy deliberately emits no
``stop_loss_price``/``take_profit_price`` on its buy signals, so the OCO
bracket that :class:`src.execution.reconciler.Reconciler` would otherwise arm
once an entry fills is never placed for a momentum-dca buy. That is a choice,
not an oversight: the stop *distance* is exactly the number step 8 of the
plan is meant to justify against the backtest, and shipping an unvalidated
one (a guessed -15%, say) risks selling a sound position early far more
reliably than it caps a loss. Until that backtest exists, the trend filter
below is the *only* downside guard this strategy carries - and it only acts
while the bot is actually running and the whole benchmark has broken trend,
not on a single name gapping down inside an intact uptrend. Reconsider this
when step 8 lands.

That trend filter: leveraged instruments (2x index funds) are only ever
bought, and are sold out of entirely, based on whether the benchmark is above
its own long moving average. A 1x position is never sold on a trend signal -
timing the DCA's core holding off a single moving-average cross has a mixed
record at best, and would strand a month's contribution in cash for no proven
benefit. A 2x position's decay is what the trend filter exists to avoid.
"""

from dataclasses import dataclass
from bisect import bisect_left
from datetime import date as date_type, timedelta
from decimal import ROUND_DOWN, Decimal

from src.models import ZERO
from src.strategy.base import ORDER_MARKET, SIDE_BUY, SIDE_SELL, Signal, Strategy
from src.strategy.indicators import drawdown_from_high, realized_vol, sma, total_return
from src.strategy.universe import BUCKET_SAFE, DEFAULT_UNIVERSE, parse_universe
from src.toss.errors import TossConfigError

ONE = Decimal("1")
CENT = Decimal("0.01")

MODE_WEEKLY = "weekly"
MODE_DISLOCATION = "dislocation"


class StrategyParamError(ValueError):
    """The strategy's own parameters are internally inconsistent."""


def _dec(value, name):
    try:
        return Decimal(str(value))
    except Exception:
        raise TossConfigError(f"strategy_params.{name} 값을 숫자로 읽을 수 없습니다: {value!r}") from None


@dataclass(frozen=True)
class MomentumDcaParams:
    """Every number this strategy uses, named, defaulted, and overridable.

    Defaults are the agreed starting point, not a claim they are optimal -
    step 8 of the plan is running these through the backtest, in and out of
    sample, before trusting any of them with real money.
    """

    # --- ranking ---
    #: (lookback_days, weight) pairs. Blends a ~3-month, ~6-month and ~1-year
    #: view rather than betting the rank on any single window.
    lookbacks: tuple = ((63, Decimal("0.5")), (126, Decimal("0.3")), (252, Decimal("0.2")))
    #: Most recent week excluded from every lookback - short-term reversal is
    #: common enough that the last few days often point the wrong way.
    skip_days: int = 5
    vol_adjust: bool = True
    vol_window: int = 63
    #: Negative momentum is never a buy candidate, regardless of rank.
    min_score: Decimal = Decimal("0")

    # --- concentration ---
    top_n: int = 2
    weights: tuple = (Decimal("0.65"), Decimal("0.35"))
    fallback_symbol: str = "QQQ"

    # --- trend filter (the leverage gate) ---
    benchmark: str = "QQQ"
    trend_sma: int = 200
    leverage_requires_trend: bool = True
    leverage_max_vol: Decimal = Decimal("0.35")
    #: Days after an accepted trend-exit sell during which the same symbol is
    #: not sold again. ``_exit_signals`` re-derives from scratch every run, so
    #: a multi-day downtrend would otherwise re-emit yesterday's full-size
    #: sell before it has settled (US equities settle T+1..T+2), and the
    #: position still shows the shares as sellable in the meantime.
    exit_cooldown_days: int = 3

    # --- rhythm ---
    rebalance_weekday: int = 0  # Monday
    dislocation_enabled: bool = True
    dislocation_day_drop: Decimal = Decimal("-0.03")
    dislocation_sigma: Decimal = Decimal("2.5")
    dislocation_drawdown: Decimal = Decimal("0.08")
    dislocation_window: int = 20
    dislocation_cooldown_days: int = 5
    dislocation_requires_trend: bool = True
    dislocation_budget_multiple: Decimal = Decimal("2")

    # --- sizing ---
    cash_reserve: Decimal = Decimal("0.05")
    min_order_usd: Decimal = Decimal("30")
    #: Hard ceiling on how much gets deployed in a single evaluation, in USD.
    #: Without this, a few skipped weeks (no eligible candidate, a data gap)
    #: let cash build up, and the next weekly rebalance deploys *all of it* -
    #: the whole account re-priced onto one or two names in one shot. The
    #: default is a rough one-month-contribution's worth at typical KRW/USD;
    #: set this to match your actual monthly contribution size in USD.
    max_deploy_per_week_usd: Decimal | None = Decimal("700")

    #: A feed this many days stale is treated as absent, not as "the last
    #: known price".
    stale_days: int = 5

    def __post_init__(self):
        if len(self.weights) != self.top_n:
            raise StrategyParamError(
                f"weights 길이({len(self.weights)})가 top_n({self.top_n})과 다릅니다."
            )
        if sum(self.weights) > ONE:
            raise StrategyParamError(f"weights 합이 1을 넘습니다: {sum(self.weights)}")

    @property
    def required_bars(self):
        """Longest lookback this strategy ever reads, plus the skip."""
        longest = max([lb for lb, _ in self.lookbacks] + [self.trend_sma, self.vol_window])
        return longest + self.skip_days + 1

    _FIELDS = None  # set below, after the class body

    @classmethod
    def from_mapping(cls, raw):
        """Build params from ``trading_config.strategy_params``.

        Unknown keys are an error, matching ``config.py``'s convention: a
        typo'd parameter that is silently dropped looks, from the config
        file, like it is in force.
        """
        raw = raw or {}
        known = set(cls._FIELDS) | {"weights", "lookbacks"}
        unknown = set(raw) - known
        if unknown:
            raise TossConfigError(
                f"strategy_params에 알 수 없는 항목이 있습니다: {sorted(unknown)}. "
                f"사용 가능: {sorted(known)}"
            )

        kwargs = {}
        for name, kind in cls._FIELDS.items():
            if name not in raw:
                continue
            value = raw[name]
            if kind == "decimal_or_none":
                kwargs[name] = None if value is None else _dec(value, name)
            elif kind == "decimal":
                kwargs[name] = _dec(value, name)
            elif kind is bool:
                kwargs[name] = bool(value)
            elif kind is str:
                kwargs[name] = str(value)
            else:
                try:
                    kwargs[name] = int(value)
                except (TypeError, ValueError):
                    raise TossConfigError(
                        f"strategy_params.{name} 값을 정수로 읽을 수 없습니다: {value!r}"
                    ) from None

        if "weights" in raw:
            kwargs["weights"] = tuple(_dec(w, "weights") for w in raw["weights"])
        if "lookbacks" in raw:
            kwargs["lookbacks"] = tuple(
                (int(lb), _dec(w, "lookbacks")) for lb, w in raw["lookbacks"]
            )

        try:
            return cls(**kwargs)
        except StrategyParamError as exc:
            raise TossConfigError(f"strategy_params 설정 오류: {exc}") from exc


MomentumDcaParams._FIELDS = {
    "skip_days": int,
    "vol_adjust": bool,
    "vol_window": int,
    "min_score": "decimal",
    "top_n": int,
    "fallback_symbol": str,
    "benchmark": str,
    "trend_sma": int,
    "leverage_requires_trend": bool,
    "leverage_max_vol": "decimal",
    "exit_cooldown_days": int,
    "rebalance_weekday": int,
    "dislocation_enabled": bool,
    "dislocation_day_drop": "decimal",
    "dislocation_sigma": "decimal",
    "dislocation_drawdown": "decimal",
    "dislocation_window": int,
    "dislocation_cooldown_days": int,
    "dislocation_requires_trend": bool,
    "dislocation_budget_multiple": "decimal",
    "cash_reserve": "decimal",
    "max_deploy_per_week_usd": "decimal_or_none",
    "min_order_usd": "decimal",
    "stale_days": int,
}


def _benchmark_behind(ctx, benchmark_date):
    """Is the benchmark's newest bar older than the session this run acts on?

    ``today`` comes from the benchmark's last bar, but the run's session -
    and with it every order id and daily limit - is the newest bar across
    *all* symbols (``session_date_of``). When one refresh fails and another
    succeeds the two disagree, and the strategy re-decides a day that has
    already been acted on under a new session's ids, which nothing dedupes.
    That happened on 2026-09-23: Yahoo returned nothing for QQQ, SHY had
    been fetched an hour earlier, and the run proposed Monday's weekly buys
    a second time as session 9/22. Doing nothing is the safe reading of a
    stale benchmark - the next run with fresh bars catches up.
    """
    return benchmark_date is not None and benchmark_date < ctx.session_date


def _entry_date(entry):
    from src.strategy.bars import as_date

    value = entry.get("ts") or entry.get("date")
    if not value:
        return None
    try:
        return as_date(value)
    except Exception:
        return None


def _entry_meta(entry):
    meta = entry.get("meta")
    if isinstance(meta, dict):
        return meta
    payload = entry.get("payload")
    if isinstance(payload, dict):
        # The live store (``store.repo.Store.save_decision``) writes
        # ``signal.meta`` straight through as a dict under ``payload`` -
        # ``recent_signals()`` hands it back unchanged. Only the SQLite-era
        # path ever JSON-encoded it to a string; the backtest uses the
        # ``meta`` key above. Without this branch every live cooldown lookup
        # falls through to ``json.loads(dict)`` -> TypeError -> ``{}``, so no
        # dislocation buy is ever recognised and the fail-closed guard fires
        # on every dip.
        return payload
    if payload:
        import json

        try:
            return json.loads(payload)
        except (TypeError, ValueError):
            return {}
    return {}


def _require_weight_caps_fit_the_gate(universe, limits):
    """Fail at load time if this strategy plans to hold more than the gate
    allows, rather than letting it surface as position-weight-limit rejections
    on every single order once running.

    ``Instrument.max_weight`` is the strategy's *plan* for how concentrated a
    holding may get; the risk gate's ``max_position_weight`` (and its
    per-symbol overrides) is the *enforced* ceiling. A plan above the ceiling
    is not a strategy decision the gate quietly narrows down to size - it is
    a configuration mismatch, and the two should be reconciled before the
    account is trading, not discovered from a wall of rejections in
    production days later.
    """
    for instrument in universe.enabled():
        cap = limits.max_position_weight_overrides.get(
            instrument.symbol, limits.max_position_weight
        )
        if instrument.max_weight > cap:
            raise TossConfigError(
                f"{instrument.symbol}의 전략 목표 비중({instrument.max_weight:.1%})이 "
                f"리스크 게이트 한도({cap:.1%})를 넘습니다. config.yaml의 "
                "trading.limits.max_position_weight_overrides에 "
                f"{instrument.symbol}: {instrument.max_weight} 이상을 추가하세요."
            )


class MomentumDcaStrategy(Strategy):
    """Rank the universe by risk-adjusted momentum; concentrate new cash into
    the top names; gate leverage on trend; exit leverage when trend breaks."""

    name = "momentum-dca"

    def __init__(self, universe=None, params=None):
        self.universe = universe or DEFAULT_UNIVERSE
        self.params = params or MomentumDcaParams()

    @classmethod
    def from_config(cls, trading_config=None):
        universe_rows = getattr(trading_config, "universe", None) if trading_config else None
        params_raw = getattr(trading_config, "strategy_params", None) if trading_config else None
        universe = parse_universe(universe_rows)
        if trading_config is not None:
            _require_weight_caps_fit_the_gate(universe, trading_config.risk_limits())
        return cls(universe=universe, params=MomentumDcaParams.from_mapping(params_raw))

    # ---------------------------------------------------------- evaluate

    def evaluate(self, ctx):
        p = self.params

        benchmark_history = ctx.bars(p.benchmark)
        # Anchor every date comparison - the weekly rebalance weekday, the
        # dislocation cooldown, staleness - to the exchange session, not the
        # wall clock this happens to run on. A post-close run in KST is
        # already on the next calendar day while the US session that just
        # closed is the one to rebalance for; keying off ``ctx.now`` there
        # silently skips that week entirely. The benchmark's last completed
        # bar *is* that session date, and in the backtest it equals
        # ``ctx.now``'s date exactly, so this changes nothing there.
        session_date = benchmark_history.last_date if benchmark_history is not None else None
        if _benchmark_behind(ctx, session_date):
            return []
        today = session_date or (ctx.now.date() if hasattr(ctx.now, "date") else ctx.now)
        trend_up, bench_closes = self._trend(benchmark_history, today, p)
        if trend_up is None:
            # No benchmark, no trend filter, no trading - the leverage gate
            # cannot be applied to data that does not exist.
            return []

        signals = list(self._exit_signals(ctx, today, trend_up))

        mode = self._mode(ctx, today, benchmark_history, trend_up, bench_closes, p)
        if mode is None:
            return signals

        signals.extend(self._buy_signals(ctx, today, trend_up, bench_closes, mode, p))
        return signals

    # ------------------------------------------------------------- trend

    def _trend(self, history, today, p):
        """(is_above_its_sma, closes) for the benchmark, or (None, ()) if unknown.

        ``None`` - not ``False`` - means "cannot tell", and every caller below
        treats "cannot tell" as a reason to do nothing with leverage, not as
        permission.
        """
        if history is None or history.is_stale(today, p.stale_days):
            return None, ()
        closes = history.closes()
        average = sma(closes, p.trend_sma)
        if average is None:
            return None, closes
        return closes[-1] > average, closes

    # -------------------------------------------------------------- exits

    def _exit_signals(self, ctx, today, trend_up):
        """Sell every held leveraged instrument when the trend breaks.

        Only instruments this strategy's universe marks as leveraged are
        touched - a leveraged fund the account holds from before this
        strategy existed, but which is not in the universe (single-stock
        leverage, say), is left alone; this strategy does not manage it.
        """
        if trend_up is not False:  # None (unknown) or True: no forced exit
            return
        for symbol, position in ctx.positions.items():
            instrument = self.universe.get(symbol)
            if instrument is None or not instrument.is_leveraged:
                continue
            if position.quantity <= ZERO:
                continue
            if self._exit_in_flight(ctx, symbol, today):
                continue
            yield Signal(
                strategy=self.name,
                symbol=symbol,
                side=SIDE_SELL,
                order_type=ORDER_MARKET,
                quantity=position.quantity,
                currency=position.currency,
                reason=(
                    f"추세 이탈 — {self.params.benchmark}가 {self.params.trend_sma}일선 "
                    "아래로 내려가 레버리지 포지션을 청산합니다."
                ),
                meta={"mode": "trend-exit", "benchmark": self.params.benchmark},
            )

    def _exit_in_flight(self, ctx, symbol, today):
        """True when a trend-exit sell for ``symbol`` was accepted recently
        enough that it may not have settled yet.

        ``_exit_signals`` re-derives from a clean slate every evaluation, so a
        downtrend lasting several sessions would re-propose the same
        full-quantity sell each day. Live settlement lag (T+1..T+2) leaves the
        position - and therefore the risk gate's sellable check - showing
        those shares while the prior sell is in flight, so nothing downstream
        catches the duplicate. ``ctx.recent`` is the only in-flight signal
        state a pure strategy is allowed to read.
        """
        horizon = self.params.exit_cooldown_days
        if horizon <= 0:
            return False
        for entry in ctx.recent or ():
            if not isinstance(entry, dict):
                continue
            if entry.get("strategy") != self.name or entry.get("symbol") != symbol:
                continue
            if entry.get("outcome") == "rejected":
                continue  # that sell did not happen - a retry is wanted
            if _entry_meta(entry).get("mode") != "trend-exit":
                continue
            entry_date = _entry_date(entry)
            if entry_date is None:
                return True  # undateable - assume it is still in flight
            if (today - entry_date).days < horizon:
                return True
        return False

    # --------------------------------------------------------------- mode

    def _mode(self, ctx, today, benchmark_history, trend_up, bench_closes, p):
        if self._is_rebalance_session(ctx, today, benchmark_history, p):
            return MODE_WEEKLY
        if p.dislocation_enabled and self._dislocation_fires(
            ctx, today, trend_up, bench_closes, p
        ):
            return MODE_DISLOCATION
        return None

    def _is_rebalance_session(self, ctx, today, benchmark_history, p):
        """True when this session is the week's buying day.

        The rhythm is anchored to a weekday, but exchanges take holidays and
        a weekday comparison alone drops those weeks in silence - US Labor Day
        is a Monday, so a Monday cadence simply does not buy that week and
        nothing anywhere says why. The rule is therefore *the rebalance
        weekday, or the first session after it*: the week still gets its one
        buy, one session late.

        The benchmark's own bars are the trading calendar. They are the same
        series every other date decision here is made from, they already
        exclude holidays by construction, and they need no separate calendar
        feed that could disagree with the prices being traded on.
        """
        if not isinstance(today, date_type):
            # No session date to reason about; fall back to the wall clock,
            # which is what this did before the calendar was consulted.
            return ctx.now.weekday() == p.rebalance_weekday

        # This week's rebalance weekday, on or before today.
        anchor = today - timedelta(days=(today.weekday() - p.rebalance_weekday) % 7)
        if today == anchor:
            return True

        sessions = benchmark_history.dates if benchmark_history is not None else ()
        index = bisect_left(sessions, anchor)
        if index >= len(sessions):
            # The anchor is past every bar we hold. Nothing to defer to, and
            # claiming this session is the make-up day would be a guess.
            return False
        # Only the *first* session on or after the anchor is the make-up day.
        # Later ones in the same week already had theirs.
        return sessions[index] == today

    def _dislocation_fires(self, ctx, today, trend_up, bench_closes, p):
        if not bench_closes:
            return False
        if p.dislocation_requires_trend and trend_up is not True:
            return False

        day_return = total_return(bench_closes, 1, skip=0)
        drawdown = drawdown_from_high(bench_closes, p.dislocation_window)
        if day_return is None or drawdown is None:
            return False
        if drawdown < p.dislocation_drawdown:
            return False

        daily_vol = realized_vol(bench_closes, p.vol_window, annualize=False)
        crashed = day_return <= p.dislocation_day_drop
        if not crashed and daily_vol is not None:
            crashed = day_return <= -p.dislocation_sigma * daily_vol
        if not crashed:
            return False

        last_dislocation, oldest_known = self._last_dislocation_date(ctx)
        if last_dislocation is not None:
            if (today - last_dislocation).days < p.dislocation_cooldown_days:
                return False
        elif oldest_known is not None and (today - oldest_known).days < p.dislocation_cooldown_days:
            # No dislocation buy found in ctx.recent, but the log's own
            # oldest entry doesn't reach back a full cooldown window - it may
            # have been truncated (the live path passes a fixed-size window
            # via store.recent_signals()) rather than genuinely empty of one.
            # "Can't verify" is treated as "cooldown is active", the same
            # strict-by-default posture the risk gate takes for missing
            # data - fail-open here would let a truncated log re-arm a
            # double-budget dislocation buy days early.
            return False
        return True

    def _last_dislocation_date(self, ctx):
        """(most recent dislocation buy by this strategy, oldest entry seen).

        The second value lets the caller tell "no dislocation buy happened"
        apart from "we don't have enough history to know" - an empty or
        short ``ctx.recent`` must not read as a clean cooldown.
        """
        latest = None
        oldest = None
        for entry in ctx.recent or ():
            if not isinstance(entry, dict):
                continue
            entry_date = _entry_date(entry)
            if entry_date is not None and (oldest is None or entry_date < oldest):
                oldest = entry_date
            if entry.get("strategy") != self.name:
                continue
            if _entry_meta(entry).get("mode") != MODE_DISLOCATION:
                continue
            if entry_date is not None and (latest is None or entry_date > latest):
                latest = entry_date
        return latest, oldest

    # ---------------------------------------------------------- ranking

    def _rank(self, ctx, today, trend_up, p):
        """Eligible instruments in descending score order, with their score."""
        allow_leverage = self._leverage_allowed(ctx, trend_up, p)
        ranked = []
        for instrument in self.universe.tradable(allow_leverage=True):
            if instrument.bucket == BUCKET_SAFE:
                # This strategy has no notion of holding something to a
                # target weight, so a safe asset can only reach it as a
                # ranked candidate - and momentum ranks an asset held for
                # how it behaves when equities fall in exactly the weeks it
                # is doing its job. Before the universe grew a SAFE bucket
                # there was nothing here to skip; skipping it keeps this
                # strategy behaving as it did, rather than quietly acquiring
                # a taste for Treasuries because a different strategy needed
                # them in the list.
                continue
            if instrument.is_leveraged and not allow_leverage:
                continue
            history = ctx.bars(instrument.symbol)
            if history is None or history.is_stale(today, p.stale_days):
                continue
            if len(history) < p.required_bars:
                continue
            score = self._score(history.closes(), p)
            if score is None or score <= p.min_score:
                continue
            ranked.append((instrument, score))
        ranked.sort(key=lambda pair: pair[1], reverse=True)
        return ranked

    def _leverage_allowed(self, ctx, trend_up, p):
        if p.leverage_requires_trend and trend_up is not True:
            return False
        benchmark_history = ctx.bars(p.benchmark)
        if benchmark_history is None:
            return False
        vol = realized_vol(benchmark_history.closes(), p.vol_window)
        # Unverifiable volatility is treated as "too risky to lever", the same
        # strict-by-default posture the risk gate takes for missing data.
        return vol is not None and vol <= p.leverage_max_vol

    def _score(self, closes, p):
        score = ZERO
        for lookback, weight in p.lookbacks:
            r = total_return(closes, lookback, skip=p.skip_days)
            if r is None:
                return None
            score += weight * r
        if p.vol_adjust:
            vol = realized_vol(closes, p.vol_window)
            if not vol:
                return None
            score = score / vol
        return score

    # ------------------------------------------------------------ sizing

    def _buy_signals(self, ctx, today, trend_up, bench_closes, mode, p):
        ranked = self._rank(ctx, today, trend_up, p)
        allocations = self._allocate(ctx, today, trend_up, ranked, p)
        if not allocations:
            return

        budget = self._budget(ctx, mode, p)
        if budget < p.min_order_usd:
            return

        for instrument, weight, score in allocations:
            amount = (weight * budget).quantize(CENT, rounding=ROUND_DOWN)
            amount = self._clip_to_max_weight(ctx, instrument, amount)
            if amount < p.min_order_usd:
                continue
            yield Signal(
                strategy=self.name,
                symbol=instrument.symbol,
                side=SIDE_BUY,
                order_type=ORDER_MARKET,
                amount=amount,
                currency="USD",
                reason=(
                    f"{mode} 배분 — {instrument.symbol} 점수 {score:.4f} "
                    f"({', '.join(str(lb) for lb, _ in p.lookbacks)}일 가중 모멘텀)"
                ),
                meta={
                    "mode": mode,
                    "score": str(score),
                    "weight": str(weight),
                    "budget_usd": str(budget),
                    "trend_up": trend_up,
                },
            )

    def _allocate(self, ctx, today, trend_up, ranked, p):
        top = ranked[: p.top_n]
        allocations = [
            (instrument, weight, score)
            for (instrument, score), weight in zip(top, p.weights)
        ]

        remaining_weight = sum(p.weights[len(top) :])
        already = {instrument.symbol for instrument, _, _ in allocations}
        if remaining_weight > ZERO and p.fallback_symbol not in already:
            fallback = self.universe.get(p.fallback_symbol)
            if fallback is not None and fallback.bucket != BUCKET_SAFE:
                allow_leverage = self._leverage_allowed(ctx, trend_up, p)
                if not fallback.is_leveraged or allow_leverage:
                    history = ctx.bars(fallback.symbol)
                    if (
                        history is not None
                        and not history.is_stale(today, p.stale_days)
                        and len(history) >= p.required_bars
                    ):
                        score = self._score(history.closes(), p)
                        # The fallback is a slot filler, not an exemption from
                        # the absolute-momentum gate: in a genuine downturn
                        # every ranked candidate can legitimately fail
                        # min_score, and routing the unfilled weight into an
                        # unvetted (possibly deeply negative-momentum)
                        # fallback would spend the whole budget on exactly
                        # the instrument the gate exists to keep out.
                        if score is not None and score > p.min_score:
                            allocations.append((fallback, remaining_weight, score))
        return allocations

    def _budget(self, ctx, mode, p):
        """Cash available to deploy this evaluation.

        ``cash_reserve`` is a floor on cash, not a starting point to multiply
        up from - it is computed once here and never exceeded, in either
        mode. Applying the dislocation multiplier to the reserve-adjusted
        figure and then re-clamping to raw ``usd_power`` (an earlier version
        of this method did exactly that) silently cancels the reserve on
        every dislocation buy; the fix is to only ever narrow ``spendable``,
        never widen past it.
        """
        usd_power = ctx.buying_power.get("USD") or ZERO
        if usd_power <= ZERO:
            return ZERO

        spendable = usd_power * (ONE - p.cash_reserve)

        cap = p.max_deploy_per_week_usd
        if cap is not None and mode == MODE_DISLOCATION:
            cap = cap * p.dislocation_budget_multiple

        budget = spendable if cap is None else min(spendable, cap)
        return max(budget, ZERO)

    def _clip_to_max_weight(self, ctx, instrument, amount):
        """Shrink ``amount`` so the post-buy weight stays under this
        instrument's own cap - the strategy enforcing its own plan, ahead of
        the risk gate's looser backstop (see config: max_position_weight)."""
        equity = ctx.equity_krw
        rate = ctx.exchange_rate
        if equity <= ZERO or not rate:
            return amount  # cannot verify - leave the risk gate to catch it

        position = ctx.position(instrument.symbol)
        held_krw = ZERO
        if position is not None:
            held_krw = ctx.to_krw(position.evaluation, position.currency) or ZERO

        cap_krw = instrument.max_weight * equity - held_krw
        if cap_krw <= ZERO:
            return ZERO
        cap_usd = (cap_krw / rate).quantize(CENT, rounding=ROUND_DOWN)
        return min(amount, cap_usd)
