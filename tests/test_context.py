"""Building a StrategyContext, and what happens when a lookup fails.

The rule under test throughout: a failed read must never widen what is
permitted. It leaves the field absent, and strict mode reads absent as
"cannot verify".
"""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from src.execution.context import build_context
from src.execution.risk import RiskGate, RiskLimits
from src.models import SOURCE_TOSS, PortfolioSnapshot, Position
from src.store.repo import Store
from src.strategy.bars import Bar, PriceHistory
from src.strategy.base import SIDE_BUY, Signal
from src.toss.errors import TossApiError

KST = timezone(timedelta(hours=9))
NOW = datetime(2026, 8, 26, 10, 0, tzinfo=KST)

CALENDAR = {
    "integrated": {
        "regularMarket": {
            "startTime": "2026-08-26T09:00:00+09:00",
            "endTime": "2026-08-26T15:30:00+09:00",
        }
    }
}


def position():
    return Position(
        symbol="005930",
        name="삼성전자",
        market_country="KR",
        currency="KRW",
        quantity=Decimal("10"),
        last_price=Decimal("70000"),
        avg_purchase_price=Decimal("65000"),
        source=SOURCE_TOSS,
    )


class FakeMarket:
    def __init__(self, fail=()):
        self.fail = set(fail)

    def _maybe_fail(self, name):
        if name in self.fail:
            raise TossApiError(500, "boom", "조회 실패")

    def prices(self, symbols):
        self._maybe_fail("prices")
        return {"005930": {"close": "70000"}}

    def price_limits(self, symbols):
        self._maybe_fail("price_limits")
        return {"005930": {"lowerLimit": "50000", "upperLimit": "90000"}}


class FakeAccount:
    def __init__(self, fail=()):
        self.fail = set(fail)
        self.calls = []

    def sellable_quantity(self, symbol):
        self.calls.append(symbol)
        if "sellable" in self.fail:
            raise TossApiError(500, "boom", "조회 실패")
        return {"sellableQuantity": "10"}


class FakeService:
    def __init__(self, fail=(), buying_power=None):
        self.fail = set(fail)
        self.market = FakeMarket(fail)
        self.account = FakeAccount(fail)
        self._buying_power = (
            {"KRW": "5000000", "USD": None} if buying_power is None else buying_power
        )

    def snapshot(self):
        snap = PortfolioSnapshot(
            positions=[position()],
            exchange_rate=Decimal("1400"),
            total_krw=Decimal("10000000"),
        )
        snap.buying_power = self._buying_power
        return snap

    def market_status(self):
        if "calendar" in self.fail:
            raise TossApiError(500, "boom", "조회 실패")
        return {"KR": CALENDAR, "US": None}


def build(tmp_path, firestore_client, fail=(), **kwargs):
    service = FakeService(fail=fail, **kwargs)
    store = Store(firestore_client)
    return build_context(service, store, now=NOW, kill_switch_path=str(tmp_path / "none"))


def signal():
    return Signal(
        strategy="t",
        symbol="005930",
        side=SIDE_BUY,
        reason="테스트",
        quantity=Decimal("1"),
        limit_price=Decimal("70000"),
    )


# ---------------------------------------------------------------- happy path


def test_a_complete_context_passes_the_strict_gate(tmp_path, firestore_client):
    ctx = build(tmp_path, firestore_client)

    assert ctx.prices["005930"] == Decimal("70000")
    assert ctx.buying_power == {"KRW": Decimal("5000000")}  # the None is dropped
    assert ctx.sellable["005930"] == Decimal("10")
    assert ctx.price_limits["005930"] == (Decimal("50000"), Decimal("90000"))
    assert ctx.sessions["KR"].is_open is True
    assert ctx.kill_switch is False

    assert RiskGate(RiskLimits()).evaluate(signal(), ctx).approved


def test_the_close_time_survives_into_the_session(tmp_path, firestore_client):
    close = build(tmp_path, firestore_client).sessions["KR"].regular_close
    assert close == datetime(2026, 8, 26, 15, 30, tzinfo=KST)
    # Aware on both sides, so the gate's cutoff comparison does not raise.
    assert close.tzinfo is not None and NOW.tzinfo is not None


def test_a_market_with_no_calendar_is_simply_absent(tmp_path, firestore_client):
    # US came back None; strict mode then refuses to trade it rather than
    # assuming it is open.
    assert "US" not in build(tmp_path, firestore_client).sessions


def test_sellable_is_read_once_per_held_symbol(tmp_path, firestore_client):
    # ORDER_INFO drops to 3 TPS at the open, so this must not be polled.
    service = FakeService()
    store = Store(firestore_client)
    build_context(service, store, now=NOW, kill_switch_path=str(tmp_path / "none"))
    assert service.account.calls == ["005930"]


# ------------------------------------------------- failures narrow, never widen


def test_a_failed_lookup_leaves_the_field_absent_and_the_gate_rejects(tmp_path, firestore_client):
    cases = {
        "prices": "prices",
        "price_limits": "price-limits-unknown",
        "sellable": "sellable-quantity-unknown",
        "calendar": "market-session-unknown",
    }
    gate = RiskGate(RiskLimits())

    ctx = build(tmp_path, firestore_client, fail=["price_limits"])
    assert ctx.price_limits == {}
    assert gate.evaluate(signal(), ctx).rejection.rule == cases["price_limits"]

    ctx = build(tmp_path, firestore_client, fail=["calendar"])
    assert ctx.sessions == {}
    assert gate.evaluate(signal(), ctx).rejection.rule == cases["calendar"]

    ctx = build(tmp_path, firestore_client, fail=["prices"])
    assert ctx.prices == {}


def test_missing_buying_power_is_dropped_rather_than_zeroed(tmp_path, firestore_client):
    """Absent must not read as zero, and must not read as unlimited.

    Zero would look like "no cash" (a wrong but survivable answer); the real
    answer is "unknown", which strict mode turns into a refusal.
    """
    ctx = build(tmp_path, firestore_client, buying_power={"KRW": None})
    assert ctx.buying_power == {}
    rejection = RiskGate(RiskLimits()).evaluate(signal(), ctx).rejection
    assert rejection.rule == "buying-power-unknown"


def test_the_kill_switch_file_is_picked_up(tmp_path, firestore_client):
    switch = tmp_path / "KILL_SWITCH"
    switch.write_text("")
    service, store = FakeService(), Store(firestore_client)
    ctx = build_context(service, store, now=NOW, kill_switch_path=str(switch))
    assert ctx.kill_switch is True
    assert RiskGate(RiskLimits()).evaluate(signal(), ctx).rejection.rule == "kill-switch"


# ------------------------------------------------------------- session date


def history_ending(*dates):
    """``{symbol: PriceHistory}`` whose last bars fall on ``dates``."""
    return {
        f"S{i}": PriceHistory(
            symbol=f"S{i}",
            bars=[
                Bar(
                    date=d,
                    open=Decimal("100"),
                    high=Decimal("100"),
                    low=Decimal("100"),
                    close=Decimal("100"),
                )
            ],
        )
        for i, d in enumerate(dates)
    }


def test_the_session_date_is_the_last_bar_not_the_wall_clock(tmp_path, firestore_client):
    """The run happens on the 9th; the session it trades closed on the 8th."""
    service = FakeService()
    store = Store(firestore_client)

    ctx = build_context(
        service,
        store,
        now=datetime(2026, 9, 9, 23, 35, tzinfo=KST),
        kill_switch_path=str(tmp_path / "none"),
        history=history_ending(date(2026, 9, 8)),
    )

    assert ctx.session_date == date(2026, 9, 8)
    assert ctx.now.date() == date(2026, 9, 9)


def test_one_quiet_feed_does_not_drag_the_session_back(tmp_path, firestore_client):
    ctx = build_context(
        FakeService(),
        Store(firestore_client),
        now=NOW,
        kill_switch_path=str(tmp_path / "none"),
        history=history_ending(date(2026, 9, 8), date(2026, 9, 4)),
    )

    assert ctx.session_date == date(2026, 9, 8)


def test_with_no_history_the_session_date_falls_back_to_today(tmp_path, firestore_client):
    """Every caller with no bars loaded keeps the behaviour it always had."""
    assert build(tmp_path, firestore_client).session_date == NOW.date()
