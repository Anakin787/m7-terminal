"""Data assembly and caching for the dashboard.

The cache is not an optimisation - it is a correctness requirement. The
ACCOUNT rate limit group allows one request per second, so a couple of
browser tabs polling every few seconds would trip 429 immediately. The server
owns one cached copy and every tab reads that.
"""

import threading
import time
from datetime import date, datetime, timedelta
from decimal import Decimal

from src.pipeline import PortfolioService, apply_name_overrides
from src.store.repo import Store
from src.toss.calendar import live_session
from src.toss.errors import TossError

#: Seconds each kind of data stays fresh. Holdings move slowly enough that a
#: minute is imperceptible; the calendar changes a couple of times a day.
TTL_PORTFOLIO = 60
TTL_MARKET_STATUS = 300
#: The freshness check reads the store, and the thing it watches for moves on
#: the scale of a day, so it does not need to be re-read on every 15s poll.
TTL_FRESHNESS = 300

#: A snapshot older than this means the daily job has not run. Set a little
#: over a day so a normal weekday gap - the job runs at 10:00, someone looks
#: at 09:00 - does not raise it, while a genuinely skipped run does.
SNAPSHOT_STALE_HOURS = 26


class _Cached:
    def __init__(self, ttl):
        self.ttl = ttl
        self.value = None
        self.error = None
        self.fetched_at = 0.0
        self.lock = threading.Lock()

    def get(self, loader):
        with self.lock:
            if self.value is not None and time.monotonic() - self.fetched_at < self.ttl:
                return self.value, self.error
            try:
                self.value = loader()
                self.error = None
            except TossError as exc:
                # Keep serving the stale value; the UI shows the error banner
                # rather than going blank on one transient failure.
                self.error = str(exc)
            self.fetched_at = time.monotonic()
            return self.value, self.error

    def invalidate(self):
        with self.lock:
            self.fetched_at = 0.0


def _num(value):
    """JSON-safe number. Decimal is not serialisable and float loses won."""
    if value is None:
        return None
    return float(value)


def _exchange_today():
    """Today's date on the US exchange calendar.

    The manual holdings whose daily P&L this feeds are all US-listed. Using
    the local (KST) date would roll "today" ~14h early and zero out the day's
    move for most of the Korean evening.
    """
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo("America/New_York")).date()
    except Exception:
        return datetime.now().date()


def _make_previous_close_fn():
    """A cache-first, best-effort previous-close lookup for manual holdings.

    Manual holdings carry no "today's change" field in the Toss price feed,
    so their daily P&L has to be derived against the last completed daily
    bar. Bars come from the local SQLite cache (``src/data/cache.py``, the one
    part of the store deliberately *not* in Firestore), topped up from yfinance
    only when stale. The loader is built lazily on first use so importing this
    module never pulls in yfinance or opens the cache file.
    """
    state = {}

    def previous_closes(symbols):
        loader = state.get("loader")
        if loader is None:
            from src.data.cache import BarCache
            from src.data.loader import HistoryLoader
            from src.data.yahoo import YahooBarSource

            # No staleness window: the loader now tops up whenever the cache
            # stops short of the last closed session, which is what the
            # 1-day window here was reaching for.
            loader = HistoryLoader(BarCache(), YahooBarSource())
            state["loader"] = loader

        end = date.today()
        histories = loader.load(list(symbols), end - timedelta(days=12), end)

        cutoff = _exchange_today()
        result = {}
        for symbol, history in histories.items():
            prev = None
            for bar in history:  # oldest first
                if bar.date < cutoff:
                    prev = bar.close
            if prev is not None and prev > 0:
                result[symbol] = prev
        return result

    return previous_closes


class DashboardService:
    def __init__(self, config):
        self.config = config
        self.store = Store()
        self.portfolio = PortfolioService(
            config, previous_close_fn=_make_previous_close_fn()
        )
        self._snapshot = _Cached(TTL_PORTFOLIO)
        self._status = _Cached(TTL_MARKET_STATUS)
        self.last_sync = None

    def _load_snapshot(self):
        snapshot = self.portfolio.snapshot()
        apply_name_overrides(snapshot, self.store.symbol_names())
        self.last_sync = datetime.now()
        return snapshot

    def rename_symbol(self, symbol, name):
        """Set or clear a display name, then refresh so the UI shows it."""
        stored = self.store.set_symbol_name(symbol, name)
        self._snapshot.invalidate()
        return stored

    def snapshot(self):
        return self._snapshot.get(self._load_snapshot)

    def overview(self):
        snapshot, error = self.snapshot()
        status, _ = self._status.get(self.portfolio.market_status)

        if snapshot is None:
            return {"error": error or "포트폴리오를 불러오지 못했습니다.", "ready": False}

        buying_power = snapshot.buying_power or {}
        return {
            "ready": True,
            "error": error,
            "total_krw": _num(snapshot.total_krw),
            "total_usd_equivalent": _num(snapshot.total_usd_equivalent),
            "purchase_krw": _num(snapshot.purchase_krw),
            "profit_krw": _num(snapshot.profit_krw),
            "profit_rate": _num(snapshot.profit_rate),
            "profit_after_cost_krw": _num(snapshot.profit_after_cost_krw),
            "profit_rate_after_cost": _num(snapshot.profit_rate_after_cost),
            "daily_profit_krw": _num(snapshot.daily_profit_krw),
            "daily_profit_rate": _num(snapshot.daily_profit_rate),
            "exchange_rate": _num(snapshot.exchange_rate),
            "has_unconverted_fx": snapshot.has_unconverted_fx,
            "fx_pnl_krw": _num(snapshot.total_fx_pnl_krw),
            "buying_power": {
                "KRW": _num(_decimal_or_none(buying_power.get("KRW"))),
                "USD": _num(_decimal_or_none(buying_power.get("USD"))),
            },
            "warnings": snapshot.warnings,
            "market_status": _market_status(status),
        }

    def holdings(self):
        snapshot, error = self.snapshot()
        if snapshot is None:
            return {"error": error, "positions": []}

        total = snapshot.total_krw or Decimal("1")
        positions = []
        for position in snapshot.positions:
            value_krw = snapshot.evaluation_krw(position)
            cost_krw = snapshot.cost_krw(position)
            positions.append(
                {
                    "symbol": position.symbol,
                    "name": position.name,
                    "market_country": position.market_country,
                    "currency": position.currency,
                    "quantity": _num(position.quantity),
                    "last_price": _num(position.last_price),
                    "avg_price": _num(position.avg_purchase_price),
                    "value_krw": _num(value_krw),
                    "cost_krw": _num(cost_krw),
                    "profit_krw": _num(value_krw - cost_krw),
                    "fx_pnl_krw": _num(snapshot.fx_pnl_krw(position)),
                    "profit_loss": _num(position.profit_loss),
                    "profit_rate": _num(position.profit_rate),
                    "daily_profit_rate": _num(position.daily_profit_rate),
                    "weight": _num(value_krw / total) if total else 0,
                    "source": position.source,
                }
            )
        positions.sort(key=lambda item: item["value_krw"] or 0, reverse=True)
        return {"error": error, "positions": positions}

    def history(self, range_key="3M"):
        rows = self.store.history(range_key)
        return {
            "range": range_key,
            "points": [
                {
                    "ts": row["ts"],
                    "total_krw": _num(row["total_krw"]),
                    "profit_rate": _num(row["profit_rate"]),
                }
                for row in rows
            ],
            "total_snapshots": self.store.snapshot_count(),
        }

    def allocation(self, by="market"):
        snapshot, error = self.snapshot()
        if snapshot is None:
            return {"error": error, "segments": []}

        if by == "bucket":
            return self._bucket_allocation(snapshot)

        buckets = snapshot.allocation(by)
        total = sum(buckets.values()) or Decimal("1")
        segments = [
            {
                "key": key,
                "label": _allocation_label(key, by),
                "value_krw": _num(value),
                "share": _num(value / total),
            }
            for key, value in buckets.items()
        ]
        segments.sort(key=lambda item: item["value_krw"], reverse=True)
        return {"by": by, "segments": segments}

    def _bucket_allocation(self, snapshot):
        """The strategy's own shape - safe / core / growth against target.

        The other two groupings answer "where is the money"; this one answers
        "is the plan being followed", which is the only question the active
        strategy is organised around and the only one no screen could
        previously answer.
        """
        from src.strategy.loader import load_strategies
        from src.strategy.universe import UNMANAGED, parse_universe

        try:
            targets = next(
                (
                    s.params.weights
                    for s in load_strategies(self.config.trading)
                    if isinstance(
                        getattr(getattr(s, "params", None), "weights", None), dict
                    )
                ),
                None,
            )
        except Exception:  # noqa: BLE001 - a chart must not break the page
            targets = None
        if not targets:
            return {
                "by": "bucket",
                "segments": [],
                "error": "활성 전략에 버킷 계획이 없습니다.",
            }

        allocation = parse_universe(self.config.trading.universe).bucket_allocation(
            snapshot, targets=targets
        )
        order = {"SAFE": 0, "CORE": 1, "GROWTH": 2, UNMANAGED: 3}
        segments = [
            {
                "key": bucket,
                "label": _allocation_label(bucket, "bucket"),
                "value_krw": _num(row["value_krw"]),
                "share": _num(row["share"]),
                "target": _num(row["target"]) if row["target"] is not None else None,
                "symbols": row["symbols"],
                "unmanaged": bucket == UNMANAGED,
            }
            for bucket, row in sorted(
                allocation.items(), key=lambda kv: order.get(kv[0], 9)
            )
        ]
        return {"by": "bucket", "segments": segments}

    def reports(self, limit=20):
        return {"reports": self.store.recent_reports(limit)}

    def audit(self, limit=50, category=None):
        """The change log: settings edits and what the AI review did.

        Read straight through - no snapshot, no upstream call - so this page
        still answers "what changed" on a day the brokerage API is down,
        which is exactly a day someone might be asking.
        """
        return {"entries": self.store.recent_audit(limit=limit, category=category)}

    def trading_activity(self, limit=50):
        """What the engine decided, and what came out of it.

        Two lists rather than one joined view. A signal the risk gate refused
        produces no order at all, and that absence is the most useful thing
        the pair can say - joining them would either drop those rows or
        invent an empty order to hang them on. Read straight from the store,
        like ``audit``: this has to answer "did it trade?" on a day the
        brokerage API is the thing that is broken.
        """
        return {
            "signals": self.store.recent_signals(limit=limit),
            "orders": self.store.recent_orders(limit=limit),
        }

    def health(self):
        snapshot, error = self.snapshot()
        freshness = self.snapshot_freshness()
        return {
            "connected": snapshot is not None and not error,
            "error": error,
            "last_sync": self.last_sync.isoformat() if self.last_sync else None,
            "snapshot_count": self.store.snapshot_count(),
            "trading_enabled": self.config.trading.enabled,
            **freshness,
        }

    def _freshness_cache(self):
        # Resolved lazily rather than in __init__ so that a service built
        # without running __init__ (tests) still gets one.
        cache = getattr(self, "_freshness", None)
        if cache is None:
            cache = _Cached(TTL_FRESHNESS)
            self._freshness = cache
        return cache

    def snapshot_freshness(self):
        """How long since the daily job last wrote a snapshot.

        The 8-day outage in August was invisible precisely because a report
        that does not run appears nowhere: no row, no error, no log. The only
        evidence of it was the age of the newest row, so the dashboard reads
        that age out loud instead of leaving it to be noticed.
        """

        def load():
            row = self.store.latest_snapshot() or {}
            ts = row.get("ts")
            parsed = _parse_ts(ts)
            age = None
            if parsed is not None:
                age = (datetime.now() - parsed).total_seconds() / 3600
            return {
                "last_snapshot_ts": ts,
                "snapshot_age_hours": round(age, 1) if age is not None else None,
                # No snapshot at all is not stale - it is a system that has
                # never run, which the count already says. Claiming staleness
                # would point at the wrong problem.
                "snapshot_stale": age is not None and age > SNAPSHOT_STALE_HOURS,
                "snapshot_stale_after_hours": SNAPSHOT_STALE_HOURS,
            }

        try:
            value, _ = self._freshness_cache().get(load)
        except Exception:  # noqa: BLE001 - the store being down is its own alarm
            value = None
        return value or {
            "last_snapshot_ts": None,
            "snapshot_age_hours": None,
            "snapshot_stale": False,
            "snapshot_stale_after_hours": SNAPSHOT_STALE_HOURS,
        }

    # -------------------------------------------------------- kill switch

    def trading_status(self):
        """What the engine may do right now, and what is stopping it.

        Three separate facts, kept separate: the config switch, the kill
        switch file, and whether LIVE is open at all. Collapsing them into one
        "enabled" flag would answer "can it trade?" while hiding the only
        useful question when the answer is no - which of the three to change.
        """
        from src.execution.risk import kill_switch_state

        trading = self.config.trading
        state = kill_switch_state(trading.kill_switch_path, store=self.store)
        return {
            "engine_enabled": trading.enabled,
            "strategies": list(trading.strategies),
            "mode": "paper",
            # Design 6 [10]: LIVE opens only after its own verification.
            "live_open": False,
            "kill_switch": state,
            "halted": bool(state["active"]),
        }

    def set_kill_switch(self, active, reason=None, actor=None):
        """Engage or release the kill switch, and audit a real transition.

        The write happens before the audit entry: if the store is unreachable
        the engine must still stop. A stop nobody logged beats a log of a stop
        that did not happen.
        """
        from src.audit import kill_switch_entry
        from src.execution.risk import (
            engage_kill_switch,
            kill_switch_state,
            release_kill_switch,
        )

        path = self.config.trading.kill_switch_path
        was_active = kill_switch_state(path, store=self.store)["active"]

        if active:
            state = engage_kill_switch(
                path, reason=reason, actor=actor or "dashboard", store=self.store
            )
        else:
            release_kill_switch(path, store=self.store)
            state = kill_switch_state(path, store=self.store)

        if state["active"] != was_active:
            try:
                self.store.save_audit_entries(
                    [kill_switch_entry(state, state["active"], actor=actor)]
                )
            except Exception:  # noqa: BLE001 - see docstring
                pass

        return self.trading_status()

    def settings(self):
        """Read-only view of the effective config, with secrets masked."""
        from src.config import mask_secret

        return {
            "toss": {
                "client_id": mask_secret(self.config.toss.client_id),
                "client_secret": mask_secret(self.config.toss.client_secret),
                "account_no": self.config.toss.account_no,
                "base_url": self.config.toss.base_url,
            },
            "notion": {
                "configured": self.config.notion.is_configured,
                "database_id": mask_secret(self.config.notion.database_id),
            },
            "analyst": {
                "model": self.config.analyst.model,
                "configured": bool(self.config.analyst.api_key),
            },
            "manual_holdings": len(self.config.manual_holdings),
        }

    def close(self):
        self.portfolio.close()


def _parse_ts(value):
    """Parse a stored ISO timestamp, tolerating the ones we did not write."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    # Rows are written with local naive timestamps; an aware one from some
    # other writer is compared in local terms rather than crashing on it.
    return parsed.replace(tzinfo=None) if parsed.tzinfo else parsed


def _decimal_or_none(value):
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _allocation_label(key, by):
    if by == "currency":
        return key
    if by == "bucket":
        return {
            "SAFE": "안전자산",
            "CORE": "일반",
            "GROWTH": "미래성장",
            "UNMANAGED": "전략 외 보유",
        }.get(key, key)
    return {"KR": "KRX (Domestic)", "US": "US (Foreign)"}.get(key, key or "Other")


def _market_status(status):
    """Reduce the calendar payload to the live session per market."""
    result = {}
    for country in ("KR", "US"):
        calendar = (status or {}).get(country)
        session = live_session(calendar)
        result[country] = {
            # "open" stays regular-session-only so anything reading it keeps
            # meaning the main session; extended hours are reported via
            # "session" instead.
            "open": session == "regular",
            "session": session,
            "known": calendar is not None,
        }
    return result
