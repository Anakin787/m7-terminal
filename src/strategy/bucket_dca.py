"""Bucket-targeted, concentrated dollar-cost averaging.

What this changes from :mod:`src.strategy.momentum_dca`, and why.

*It holds a shape, not just a ranking.* The older strategy decided only where
*this week's* cash went, and never sold a 1x position. Ranked weekly, with the
top two rotating, that accumulated: a 2016-2026 run bought 37 of 39 names and
the top two took 18% of the money. The docstring said concentration; the
portfolio was a momentum-tilted index. Here the plan is a target share of
equity per bucket - safe / core / growth - filled by a fixed number of slots,
so what the account holds is a stated shape rather than the residue of ten
years of weekly winners.

*It sells - but only for a reason about the holding itself.* The older
strategy never sold a 1x position; holding ten names while ranking forty
requires selling something. The first build here rotated on *rank*, every
session, and that was worse than not selling at all: ranks near the cut trade
places constantly, so a name up 60% that cooled from rank 2 to rank 9 was cut
exactly like one that had broken down - the opposite of how a momentum premium
is harvested. A holding now leaves only when its own momentum is gone, meaning
it would not be bought today, and that judgement is made once a week, on the
rebalance session, because the score is built from 63/126/252-day lookbacks
and sampling a months-long signal daily reads noise.

*It ranks inside buckets, never across them.* A speculative name having a hot
quarter cannot take a core slot, and the safe bucket is not ranked at all. An
asset held for how it behaves in the weeks equities fall would be sold by
momentum in exactly those weeks; ranking it would defeat the reason it is
there.

Unchanged from the older strategy: the score itself (blended risk-adjusted
momentum over three lookbacks), the absolute-momentum floor, the leverage
trend gate, the dislocation top-up, and the absence of a per-position stop -
see that module for the reasoning behind each.
"""

from dataclasses import dataclass
from datetime import date as date_type
from decimal import ROUND_DOWN, Decimal

from src.models import ZERO
from src.strategy.base import ORDER_MARKET, SIDE_BUY, SIDE_SELL, Signal, Strategy
from src.strategy.indicators import realized_vol, sma
from src.strategy.momentum_dca import (
    CENT,
    MODE_DISLOCATION,
    MODE_WEEKLY,
    ONE,
    MomentumDcaStrategy,
    StrategyParamError,
    _benchmark_behind,
    _dec,
    _entry_meta,
)
from src.strategy.universe import (
    BUCKET_CORE,
    BUCKET_GROWTH,
    BUCKET_SAFE,
    BUCKETS,
    DEFAULT_UNIVERSE,
    parse_universe,
)
from src.toss.errors import TossConfigError

MODE_ROTATION = "rotation-exit"


def _weights_mapping(raw, name):
    """``{bucket: Decimal}`` from config, refusing an unknown bucket."""
    if not isinstance(raw, dict):
        raise TossConfigError(f"strategy_params.{name}은 매핑이어야 합니다: {raw!r}")
    out = {}
    for key, value in raw.items():
        bucket = str(key).upper()
        if bucket not in BUCKETS:
            raise TossConfigError(
                f"strategy_params.{name}에 알 수 없는 버킷이 있습니다: {key!r} "
                f"(사용 가능: {sorted(BUCKETS)})"
            )
        out[bucket] = value
    return out


@dataclass(frozen=True)
class BucketDcaParams:
    """Every number this strategy uses, named, defaulted, and overridable."""

    # --- the shape ---
    #: Target share of total equity per bucket. Must sum to 1.
    bucket_weights: tuple = (
        (BUCKET_SAFE, Decimal("0.20")),
        (BUCKET_CORE, Decimal("0.60")),
        (BUCKET_GROWTH, Decimal("0.20")),
    )
    #: How many names each bucket holds. The sum is the portfolio's size.
    bucket_slots: tuple = ((BUCKET_SAFE, 2), (BUCKET_CORE, 6), (BUCKET_GROWTH, 2))
    #: A held name is sold once it falls this far past the last slot - not the
    #: first week it slips one place. Ranks near the cut change constantly; a
    #: strategy that acted on every crossing would pay commission and capital
    #: gains tax for the privilege of buying the same name back.
    rotation_buffer: int = 2
    #: Turn rotation off entirely. The leverage trend-exit still runs - that
    #: is a risk control, not a rotation - so this is "never sell to change
    #: my mind", not "never sell".
    #:
    #: A large ``rotation_buffer`` does *not* do this. Past the length of the
    #: ranking the buffer stops mattering, and the keep-list becomes every
    #: ranked name - which is exactly what ``require_absolute_exit`` already
    #: computes. The two settings then produce identical runs, which is how
    #: this flag came to exist: a lab row labelled "매도 안 함" matched the
    #: absolute-exit row to the last decimal because it *was* that row.
    rotation_enabled: bool = True
    #: Sell only when the holding itself has stopped qualifying - not when
    #: it merely slipped down the ranking. See ``_rotation_exits``.
    require_absolute_exit: bool = True
    #: A held name keeps its slot as long as it still ranks at all.
    #:
    #: Without this the slots are re-auctioned every week to whoever is
    #: hottest, and since ``require_absolute_exit`` only sells a name whose
    #: own momentum is gone, last week's occupant keeps its shares while a
    #: new name takes the slot. The roster then grows without limit - a 2+6+2
    #: plan held 20-27 names at once - which is the same accumulation the
    #: older strategy had, reached by a different road. Incumbency is what
    #: makes the slot count a real ceiling.
    prefer_incumbents: bool = True
    #: How a bucket's share becomes per-name amounts.
    #:
    #: ``"target"`` buys toward an equal share of the bucket per slot, so a
    #: name above its share receives nothing more and the cash goes to the
    #: laggards. That is rebalancing, and against a momentum ranking it
    #: systematically starves whatever is working - measured at ~11pp a year
    #: below simply buying the same shape.
    #:
    #: ``"flow"`` splits each week's money by bucket share and lets a winner
    #: keep compounding until it reaches its own ``max_weight``. The shape is
    #: then a statement about where new money goes, not a level the portfolio
    #: is dragged back to.
    weight_mode: str = "target"
    #: Where a bucket's unfilled weight goes when it has fewer eligible names
    #: than slots - most often GROWTH, which is short by design. Left as cash
    #: it would be a permanent drag; pushed into GROWTH it would concentrate
    #: the one bucket whose names are least proven.
    unfilled_weight_to: str = BUCKET_SAFE

    # --- ranking (see momentum_dca for the reasoning) ---
    lookbacks: tuple = ((63, Decimal("0.5")), (126, Decimal("0.3")), (252, Decimal("0.2")))
    skip_days: int = 5
    vol_adjust: bool = True
    vol_window: int = 63
    min_score: Decimal = Decimal("0")

    # --- trend filter (the leverage gate) ---
    benchmark: str = "QQQ"
    trend_sma: int = 150
    leverage_requires_trend: bool = True
    leverage_max_vol: Decimal = Decimal("0.35")
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
    #: Ceiling on one evaluation's deployment, as a share of total equity.
    #:
    #: A *fixed dollar* ceiling was inherited from the buy-only strategy,
    #: where the only inflow was the monthly contribution and the cap
    #: therefore never bound. Once the strategy sells, a rotation returns a
    #: whole position - tens of thousands of dollars in a grown account - and
    #: paying it back in at $700 a week takes months while the next sale is
    #: already landing. Measured: 33% of the portfolio in idle cash on
    #: average, 61% by the end, and about 11 percentage points a year of
    #: return given up for nothing.
    #:
    #: A share of equity keeps the original intent - do not re-price the
    #: whole account onto one week's prices - while scaling with the account,
    #: so redeploying what was just sold is never the thing it throttles.
    #: ``None`` disables it and leaves only the fixed cap below.
    #: 20% is where the curve flattens: 15, 20, 30 and no cap at all return
    #: within a tenth of a point of each other, while 5% gives up 1.5 points a
    #: year. The number is kept finite rather than removed because the cap's
    #: original point - do not re-price the account onto one week's prices -
    #: is still worth something at no measured cost.
    max_deploy_per_week_pct: Decimal | None = Decimal("0.20")
    #: Fixed floor under the percentage cap, so a small account can still put
    #: a month's contribution to work. ``None`` removes it.
    max_deploy_per_week_usd: Decimal | None = Decimal("700")

    #: A challenger must beat the weakest incumbent by this much to take its
    #: slot. ``None`` means an incumbent is never displaced while it still
    #: ranks at all - it leaves only by failing the absolute test. Only read
    #: when ``prefer_incumbents`` is on.
    incumbent_margin: Decimal | None = None
    #: Also require the name to be below its own trend line before selling.
    #: A slower, more persistent condition than the score alone, standing in
    #: for "has been failing for a while" without needing stored state.
    exit_requires_trend_break: bool = True
    #: Fraction of a position sold when it does leave. Below 1 this trims
    #: rather than exits, which realises less gain per decision.
    exit_fraction: Decimal = Decimal("1")
    stale_days: int = 5

    def __post_init__(self):
        weights = dict(self.bucket_weights)
        slots = dict(self.bucket_slots)
        for bucket in weights:
            if bucket not in BUCKETS:
                raise StrategyParamError(f"알 수 없는 버킷입니다: {bucket}")
        total = sum(weights.values(), ZERO)
        if total != ONE:
            raise StrategyParamError(
                f"bucket_weights 합이 1이 아닙니다: {total}. 남는 비중이 어디로 "
                "가는지가 정해지지 않은 배분은 계획이 아닙니다."
            )
        for bucket, count in slots.items():
            if count < 0:
                raise StrategyParamError(f"{bucket}의 슬롯 수가 음수입니다: {count}")
            if weights.get(bucket, ZERO) > ZERO and count == 0:
                raise StrategyParamError(
                    f"{bucket}에 목표 비중은 있는데 슬롯이 0입니다 - 채울 수 없는 비중입니다."
                )
        if self.unfilled_weight_to not in BUCKETS:
            raise StrategyParamError(
                f"unfilled_weight_to가 버킷이 아닙니다: {self.unfilled_weight_to}"
            )
        if self.weight_mode not in ("target", "flow"):
            raise StrategyParamError(
                f"weight_mode는 'target' 또는 'flow'여야 합니다: {self.weight_mode!r}"
            )
        if not (ZERO < self.exit_fraction <= ONE):
            raise StrategyParamError(
                f"exit_fraction은 0 초과 1 이하여야 합니다: {self.exit_fraction}"
            )
        if self.rotation_buffer < 0:
            raise StrategyParamError(f"rotation_buffer는 0 이상이어야 합니다: {self.rotation_buffer}")

    @property
    def weights(self):
        return dict(self.bucket_weights)

    @property
    def slots(self):
        return dict(self.bucket_slots)

    @property
    def required_bars(self):
        longest = max([lb for lb, _ in self.lookbacks] + [self.trend_sma, self.vol_window])
        return longest + self.skip_days + 1

    _FIELDS = None  # set below

    @classmethod
    def from_mapping(cls, raw):
        raw = raw or {}
        known = set(cls._FIELDS) | {"lookbacks", "bucket_weights", "bucket_slots"}
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

        if "lookbacks" in raw:
            kwargs["lookbacks"] = tuple(
                (int(lb), _dec(w, "lookbacks")) for lb, w in raw["lookbacks"]
            )
        if "bucket_weights" in raw:
            mapping = _weights_mapping(raw["bucket_weights"], "bucket_weights")
            kwargs["bucket_weights"] = tuple(
                (bucket, _dec(value, "bucket_weights")) for bucket, value in mapping.items()
            )
        if "bucket_slots" in raw:
            mapping = _weights_mapping(raw["bucket_slots"], "bucket_slots")
            kwargs["bucket_slots"] = tuple(
                (bucket, int(value)) for bucket, value in mapping.items()
            )

        try:
            return cls(**kwargs)
        except StrategyParamError as exc:
            raise TossConfigError(f"strategy_params 설정 오류: {exc}") from exc


BucketDcaParams._FIELDS = {
    "rotation_buffer": int,
    "require_absolute_exit": bool,
    "rotation_enabled": bool,
    "prefer_incumbents": bool,
    "weight_mode": str,
    "unfilled_weight_to": str,
    "skip_days": int,
    "vol_adjust": bool,
    "vol_window": int,
    "min_score": "decimal",
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
    "max_deploy_per_week_pct": "decimal_or_none",
    "incumbent_margin": "decimal_or_none",
    "exit_requires_trend_break": bool,
    "exit_fraction": "decimal",
    "min_order_usd": "decimal",
    "stale_days": int,
}


class BucketDcaStrategy(MomentumDcaStrategy):
    """Hold a target shape across safe / core / growth, filled by momentum.

    Inherits the scoring, trend, dislocation and rebalance-session machinery
    from :class:`~src.strategy.momentum_dca.MomentumDcaStrategy`; what differs
    is selection (per bucket, not one global top-N), exits (rotation as well
    as trend break) and sizing (toward a target weight, not a fixed split of
    this week's cash).
    """

    name = "bucket-dca"

    def __init__(self, universe=None, params=None):
        self.universe = universe or DEFAULT_UNIVERSE
        self.params = params or BucketDcaParams()

    @classmethod
    def from_config(cls, trading_config=None):
        from src.strategy.momentum_dca import _require_weight_caps_fit_the_gate

        universe_rows = getattr(trading_config, "universe", None) if trading_config else None
        params_raw = getattr(trading_config, "strategy_params", None) if trading_config else None
        universe = parse_universe(universe_rows)
        if trading_config is not None:
            _require_weight_caps_fit_the_gate(universe, trading_config.risk_limits())
        return cls(universe=universe, params=BucketDcaParams.from_mapping(params_raw))

    # ---------------------------------------------------------- evaluate

    def evaluate(self, ctx):
        p = self.params

        benchmark_history = ctx.bars(p.benchmark)
        session_date = benchmark_history.last_date if benchmark_history is not None else None
        if _benchmark_behind(ctx, session_date):
            return []  # see _benchmark_behind: a stale benchmark re-decides a past day
        today = session_date or (ctx.now.date() if hasattr(ctx.now, "date") else ctx.now)
        trend_up, bench_closes = self._trend(benchmark_history, today, p)
        if trend_up is None:
            return []

        # The leverage trend-exit is a risk control and stays daily: a broken
        # benchmark trend is not news that can wait for Monday.
        signals = list(self._exit_signals(ctx, today, trend_up))

        mode = self._mode(ctx, today, benchmark_history, trend_up, bench_closes, p)
        if mode is None:
            # Nothing to rank for. Scoring the whole universe on a day this
            # strategy does not act on is pure cost - and ranking daily is
            # what made rotation act on daily noise in the first place.
            return signals

        ranked = self._ranked_by_bucket(ctx, today, trend_up, p)

        # Rotation is a weekly decision, never a dislocation-day one. The
        # score is built from 63/126/252-day lookbacks - a statement about
        # months - and sampling it every session turned it into noise: an
        # earlier build re-ranked daily and sold a name the day it slipped
        # one place, then bought it back. Selling into the crash a
        # dislocation top-up exists to buy would be worse still.
        if mode == MODE_WEEKLY:
            signals.extend(self._rotation_exits(ctx, today, ranked, p))

        signals.extend(self._buy_signals(ctx, today, trend_up, ranked, mode, p))
        return signals

    # ---------------------------------------------------------- selection

    def _ranked_by_bucket(self, ctx, today, trend_up, p):
        """``{bucket: [(instrument, score), ...]}``, best first within each.

        SAFE is returned in declaration order with a ``None`` score: it is
        held to a target, not ranked. Its members still have to clear the
        staleness check - a safe asset whose feed has gone quiet is not a
        safe asset, it is an unknown one.
        """
        allow_leverage = self._leverage_allowed(ctx, trend_up, p)
        ranked = {}
        for bucket in BUCKETS:
            rows = []
            for instrument in self.universe.by_bucket(bucket, allow_leverage=True):
                if instrument.is_leveraged and not allow_leverage:
                    continue
                history = ctx.bars(instrument.symbol)
                if history is None or history.is_stale(today, p.stale_days):
                    continue
                if bucket == BUCKET_SAFE:
                    rows.append((instrument, None))
                    continue
                if len(history) < p.required_bars:
                    continue
                score = self._score(history.closes(), p)
                if score is None or score <= p.min_score:
                    continue
                rows.append((instrument, score))
            if bucket != BUCKET_SAFE:
                rows.sort(key=lambda pair: pair[1], reverse=True)
            ranked[bucket] = rows
        return ranked

    def _selected(self, ranked, p, ctx=None):
        """``{bucket: [(instrument, score)]}`` trimmed to each bucket's slots.

        With ``prefer_incumbents`` the slots go first to names already held -
        in their own ranked order, and only those still ranked at all, since
        anything that failed ``min_score`` is absent from ``ranked`` and has
        been sold. Challengers take whatever is left over. This is what keeps
        the slot count from being a ceiling on paper only.
        """
        slots = p.slots
        out = {}
        for bucket, rows in ranked.items():
            limit = slots.get(bucket, 0)
            if not p.prefer_incumbents or ctx is None:
                out[bucket] = rows[:limit]
                continue
            incumbents, challengers = [], []
            for row in rows:
                position = ctx.positions.get(row[0].symbol)
                held = position is not None and position.quantity > ZERO
                (incumbents if held else challengers).append(row)

            picked = (incumbents + challengers)[:limit]
            margin = p.incumbent_margin
            if margin is not None and incumbents and challengers:
                # A challenger may take the weakest incumbent's slot, but only
                # by a clear margin. Without one the slot changes hands on
                # noise; with one the swap has to be worth the round trip.
                picked = self._displace(incumbents, challengers, limit, margin)
            out[bucket] = picked
        return out

    @staticmethod
    def _displace(incumbents, challengers, limit, margin):
        """Incumbents keep their slots unless clearly beaten.

        Scores can be negative-ish in principle, so the margin is applied to
        the absolute size of the incumbent's score - scaling a small score by
        ``1 + margin`` would otherwise make a weak incumbent trivially easy
        (or, if negative, impossible) to displace.
        """
        held = list(incumbents[:limit])
        spare = limit - len(held)
        pool = list(challengers)
        picked = held + pool[:spare]
        pool = pool[spare:]
        if not pool:
            return picked
        best = pool[0]
        # Weakest incumbent currently occupying a slot.
        weakest = min(
            (row for row in picked if row in held), key=lambda r: r[1], default=None
        )
        if weakest is None:
            return picked
        threshold = weakest[1] + abs(weakest[1]) * margin
        if best[1] > threshold:
            picked = [row for row in picked if row is not weakest] + [best]
        return picked

    # -------------------------------------------------------------- exits

    def _rotation_exits(self, ctx, today, ranked, p):
        """Sell a held name that is no longer worth holding on its own terms.

        The reason to sell is *absolute*, not relative. A name leaves because
        its own momentum has gone - it would not be bought today - and not
        merely because two other names got better. Ranked-only rotation cut
        winners and losers alike: a holding up 60% that cooled from rank 2 to
        rank 9 was sold exactly like one that had broken down, which is the
        opposite of what a momentum premium is supposed to be harvested with.
        ``_ranked_by_bucket`` already drops anything scoring at or below
        ``min_score``, so "absent from the ranking" is that condition.

        ``require_absolute_exit=False`` restores the older rank-only rule,
        where ``rotation_buffer`` places past the last slot is enough. It is
        kept so the two can be measured against each other rather than
        argued about.

        SAFE is never rotated out either way. It is not ranked, and the weeks
        it looks worst are the weeks it is doing its job.
        """
        if not p.rotation_enabled:
            return
        slots = p.slots
        # A challenger that took an incumbent's slot has to take its money
        # too. Without this the margin is not a rotation rule at all: the
        # displaced name stops receiving cash but keeps its shares, so every
        # displacement adds a name to the roster instead of replacing one -
        # measured, a 9-name plan drifted to 15-17 as soon as a margin was
        # set. Only meaningful with a margin: under strict incumbency a
        # ranked incumbent is never displaced in the first place.
        displaced = set()
        if p.prefer_incumbents and p.incumbent_margin is not None:
            selected = self._selected(ranked, p, ctx)
            for bucket, rows in ranked.items():
                if bucket == BUCKET_SAFE:
                    continue
                chosen = {i.symbol for i, _ in selected.get(bucket, ())}
                for instrument, _ in rows:
                    symbol = instrument.symbol
                    if symbol in chosen:
                        continue
                    position = ctx.positions.get(symbol)
                    if position is not None and position.quantity > ZERO:
                        displaced.add(symbol)

        for bucket, rows in ranked.items():
            if bucket == BUCKET_SAFE:
                continue
            ranked_symbols = {instrument.symbol for instrument, _ in rows}
            if p.require_absolute_exit:
                keep = ranked_symbols - displaced
            else:
                keep = {
                    instrument.symbol
                    for instrument, _ in rows[: slots.get(bucket, 0) + p.rotation_buffer]
                } - displaced
            for instrument in self.universe.by_bucket(bucket):
                symbol = instrument.symbol
                if symbol in keep:
                    continue
                position = ctx.positions.get(symbol)
                if position is None or position.quantity <= ZERO:
                    continue
                if self._rotation_in_flight(ctx, symbol, today):
                    continue
                if p.exit_requires_trend_break and not self._below_own_trend(
                    ctx, symbol, today, p
                ):
                    continue
                quantity = position.quantity
                if p.exit_fraction < ONE:
                    quantity = (position.quantity * p.exit_fraction).quantize(
                        CENT, rounding=ROUND_DOWN
                    )
                    if quantity <= ZERO:
                        continue
                yield Signal(
                    strategy=self.name,
                    symbol=symbol,
                    side=SIDE_SELL,
                    order_type=ORDER_MARKET,
                    quantity=quantity,
                    currency=position.currency,
                    reason=(
                        f"교체 매도 — {symbol}의 모멘텀이 사라져 지금은 매수 후보가 "
                        "아닙니다."
                        if p.require_absolute_exit
                        else f"교체 매도 — {symbol}이 {bucket} 버킷의 상위 "
                        f"{slots.get(bucket, 0)}+{p.rotation_buffer}위 밖으로 밀렸습니다."
                    ),
                    meta={"mode": MODE_ROTATION, "bucket": bucket},
                )

    def _below_own_trend(self, ctx, symbol, today, p):
        """True when the name itself sits under its own moving average.

        A slower condition than the momentum score, used as a stand-in for
        "this has been failing for a while" - the strategy keeps no state
        between runs, so persistence has to come from a slower indicator
        rather than from a memory of past weeks. Unknown reads as *not*
        broken: an exit is an action, and acting on data we do not have is
        the wrong direction to fail in.
        """
        history = ctx.bars(symbol)
        if history is None or history.is_stale(today, p.stale_days):
            return False
        closes = history.closes()
        average = sma(closes, p.trend_sma)
        if average is None:
            return False
        return closes[-1] < average

    def _rotation_in_flight(self, ctx, symbol, today):
        """True when a rotation sell for ``symbol`` may not have settled yet.

        Same reasoning as ``_exit_in_flight`` in the parent: this method
        re-derives from scratch every run, US equities settle T+1..T+2, and
        the position still reads as sellable in the meantime - so without
        this the same full-size sell is re-proposed every session until it
        clears.
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
                continue
            if _entry_meta(entry).get("mode") != MODE_ROTATION:
                continue
            entry_date = self._entry_date(entry)
            if entry_date is None:
                return True
            if (today - entry_date).days < horizon:
                return True
        return False

    @staticmethod
    def _entry_date(entry):
        from src.strategy.momentum_dca import _entry_date

        return _entry_date(entry)

    # ------------------------------------------------------------ sizing

    def _managed_equity(self, ctx):
        """Cash plus the holdings this strategy's universe actually covers.

        ``ctx.equity_krw`` is the whole portfolio, and for this account most
        of it sits at another broker in instruments the leverage policy
        refuses to hold - money this strategy can neither buy nor sell.
        Sizing a 20% safe bucket against that base asks for 20% of assets it
        does not control, which lands as a far larger share of the account it
        does: with 21M won held elsewhere and 10M deposited here, a 20% safe
        target reads 6.2M, or 62% of everything actually tradable.

        So the base is what the strategy manages. Holdings outside the
        universe are reported (see ``Universe.bucket_allocation``) but never
        sized against - they are somebody else's plan.
        """
        total = ZERO
        for symbol, position in ctx.positions.items():
            if self.universe.get(symbol) is None:
                continue
            total += ctx.to_krw(position.evaluation, position.currency) or ZERO

        rate = ctx.exchange_rate
        for currency, amount in (ctx.buying_power or {}).items():
            if amount and amount > ZERO:
                if currency == "KRW":
                    total += amount
                elif rate:
                    total += amount * rate
        return total

    def _clip_to_max_weight(self, ctx, instrument, amount):
        """As the parent, but against managed equity - see ``_managed_equity``."""
        equity = self._managed_equity(ctx)
        rate = ctx.exchange_rate
        if equity <= ZERO or not rate:
            return amount

        position = ctx.position(instrument.symbol)
        held_krw = ZERO
        if position is not None:
            held_krw = ctx.to_krw(position.evaluation, position.currency) or ZERO

        cap_krw = instrument.max_weight * equity - held_krw
        if cap_krw <= ZERO:
            return ZERO
        return min(amount, (cap_krw / rate).quantize(CENT, rounding=ROUND_DOWN))

    def _budget(self, ctx, mode, p):
        """Cash to deploy this evaluation, capped by a share of equity.

        Overrides the parent's fixed-dollar ceiling. See
        ``max_deploy_per_week_pct`` for why a fixed one is wrong once the
        strategy sells: it throttles money coming *back* from a sale, which
        was never what the cap was for.
        """
        usd_power = ctx.buying_power.get("USD") or ZERO
        if usd_power <= ZERO:
            return ZERO
        spendable = usd_power * (ONE - p.cash_reserve)

        caps = []
        if p.max_deploy_per_week_usd is not None:
            caps.append(p.max_deploy_per_week_usd)
        if p.max_deploy_per_week_pct is not None:
            rate = ctx.exchange_rate
            equity_krw = self._managed_equity(ctx)
            if rate and equity_krw > ZERO:
                caps.append(p.max_deploy_per_week_pct * equity_krw / rate)
        # The fixed figure is a floor for a small account, not a second
        # ceiling: taking the tighter of the two would put the account back
        # under the dollar cap it just outgrew.
        cap = max(caps) if caps else None
        if cap is not None and mode == MODE_DISLOCATION:
            cap = cap * p.dislocation_budget_multiple

        budget = spendable if cap is None else min(spendable, cap)
        return max(budget, ZERO)

    def _targets(self, ctx, ranked, p):
        """``{symbol: target_krw}`` - the shape this strategy is aiming at.

        A bucket short of eligible names hands its unfilled share to
        ``unfilled_weight_to`` rather than leaving it in cash or crowding it
        into the bucket that could not fill itself.
        """
        equity = self._managed_equity(ctx)
        if equity <= ZERO:
            return {}

        shares = self._bucket_shares(ranked, p, ctx)
        targets = {}
        for _bucket, share, rows in shares:
            per_name = (share * equity) / Decimal(len(rows))
            for instrument, _ in rows:
                cap_krw = instrument.max_weight * equity
                targets[instrument.symbol] = min(per_name, cap_krw)
        return targets

    def _bucket_shares(self, ranked, p, ctx):
        """``[(bucket, share_of_equity, rows)]`` for every bucket that can fill.

        A bucket short of eligible names hands its unfilled share to
        ``unfilled_weight_to`` rather than leaving it in cash or crowding it
        into the bucket that could not fill itself.
        """
        selected = self._selected(ranked, p, ctx)
        weights = p.weights
        slots = p.slots

        spare = ZERO
        filled = {}
        for bucket, rows in selected.items():
            want = slots.get(bucket, 0)
            have = len(rows)
            share = weights.get(bucket, ZERO)
            if want <= 0 or share <= ZERO:
                continue
            if have == 0:
                spare += share
                continue
            filled[bucket] = rows
            if have < want:
                spare += share * (Decimal(want - have) / Decimal(want))

        out = []
        for bucket, rows in filled.items():
            share = weights.get(bucket, ZERO) * (Decimal(len(rows)) / Decimal(slots[bucket]))
            if bucket == p.unfilled_weight_to:
                share += spare
            out.append((bucket, share, rows))
        return out

    def _buy_signals(self, ctx, today, trend_up, ranked, mode, p):
        if p.weight_mode == "flow":
            yield from self._flow_buys(ctx, trend_up, ranked, mode, p)
            return
        yield from self._target_buys(ctx, trend_up, ranked, mode, p)

    def _flow_buys(self, ctx, trend_up, ranked, mode, p):
        """Split this week's money by bucket share; let winners keep growing.

        No per-name target to be dragged back to - only the instrument's own
        ``max_weight``, which is a risk limit rather than a rebalancing
        level. The shape here is a claim about where new money goes, which
        still moves the portfolio toward it over time without ever selling a
        holding for being large.
        """
        budget = self._budget(ctx, mode, p)
        if budget < p.min_order_usd:
            return

        for bucket, share, rows in self._bucket_shares(ranked, p, ctx):
            if share <= ZERO or not rows:
                continue
            per_name = (share * budget / Decimal(len(rows))).quantize(
                CENT, rounding=ROUND_DOWN
            )
            if per_name < p.min_order_usd:
                continue
            for instrument, score in rows:
                amount = self._clip_to_max_weight(ctx, instrument, per_name)
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
                        f"{mode} 배분 — {bucket} 버킷 {instrument.symbol} "
                        f"(버킷 몫 {share:.0%}를 {len(rows)}종목에 분배)"
                    ),
                    meta={
                        "mode": mode,
                        "bucket": bucket,
                        "score": str(score) if score is not None else None,
                        "bucket_share": str(share),
                        "budget_usd": str(budget),
                        "trend_up": trend_up,
                    },
                )

    def _target_buys(self, ctx, trend_up, ranked, mode, p):
        targets = self._targets(ctx, ranked, p)
        if not targets:
            return

        rate = ctx.exchange_rate
        if not rate:
            return  # cannot size a KRW target into a USD order

        gaps = {}
        for symbol, target_krw in targets.items():
            position = ctx.position(symbol)
            held_krw = ZERO
            if position is not None:
                held_krw = ctx.to_krw(position.evaluation, position.currency) or ZERO
            gap = target_krw - held_krw
            if gap > ZERO:
                gaps[symbol] = gap
        if not gaps:
            return

        budget = self._budget(ctx, mode, p)
        if budget < p.min_order_usd:
            return

        total_gap = sum(gaps.values(), ZERO)
        for symbol, gap in sorted(gaps.items(), key=lambda kv: kv[1], reverse=True):
            instrument = self.universe[symbol]
            share = gap / total_gap
            amount = (share * budget).quantize(CENT, rounding=ROUND_DOWN)
            # Never overshoot the gap itself: a week's budget larger than what
            # the shape is short of should stop at the shape, not sail past it.
            gap_usd = (gap / rate).quantize(CENT, rounding=ROUND_DOWN)
            amount = min(amount, gap_usd)
            if amount < p.min_order_usd:
                continue
            yield Signal(
                strategy=self.name,
                symbol=symbol,
                side=SIDE_BUY,
                order_type=ORDER_MARKET,
                amount=amount,
                currency="USD",
                reason=(
                    f"{mode} 배분 — {instrument.bucket} 버킷 {symbol}, "
                    f"목표까지 {gap:,.0f}원 부족"
                ),
                meta={
                    "mode": mode,
                    "bucket": instrument.bucket,
                    "target_krw": str(targets[symbol]),
                    "gap_krw": str(gap),
                    "budget_usd": str(budget),
                    "trend_up": trend_up,
                },
            )
