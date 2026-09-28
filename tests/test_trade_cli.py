"""End-to-end through trade.py, with no network and no credentials."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

import trade
from src.config import AppConfig, NotionConfig, TossConfig, TradingConfig
from src.config import AnalystConfig
from src.models import SOURCE_TOSS, PortfolioSnapshot, Position
from src.store.repo import Store
from src.strategy.base import SIDE_BUY, Signal, Strategy

KST = timezone(timedelta(hours=9))

CALENDAR = {
    "regularMarket": {
        "startTime": "2026-08-26T09:00:00+09:00",
        "endTime": "2026-08-26T15:30:00+09:00",
    }
}


class BuyOne(Strategy):
    name = "buy-one"

    def evaluate(self, ctx):
        return [
            Signal(
                strategy=self.name,
                symbol="005930",
                side=SIDE_BUY,
                reason="테스트 신호",
                quantity=Decimal("1"),
                limit_price=Decimal("70000"),
            )
        ]


class FakeService:
    def __init__(self, config=None):
        self.closed = False
        self.market = self
        self.account = self

    def snapshot(self, **kwargs):
        snap = PortfolioSnapshot(
            positions=[
                Position(
                    symbol="005930",
                    name="삼성전자",
                    market_country="KR",
                    currency="KRW",
                    quantity=Decimal("10"),
                    last_price=Decimal("70000"),
                    avg_purchase_price=Decimal("65000"),
                    source=SOURCE_TOSS,
                )
            ],
            exchange_rate=Decimal("1400"),
            total_krw=Decimal("10000000"),
        )
        snap.buying_power = {"KRW": "5000000"}
        return snap

    def prices(self, symbols):
        return {"005930": {"close": "70000"}}

    def price_limits(self, symbols):
        return {"005930": {"lowerLimit": "50000", "upperLimit": "90000"}}

    def sellable_quantity(self, symbol):
        return {"sellableQuantity": "10"}

    def market_status(self):
        return {"KR": CALENDAR}

    def resolve_account_seq(self):
        return 1

    def close(self):
        self.closed = True


class FakeTrading:
    """Stands in for TradingApi so no client is ever constructed."""

    def __init__(self, *args, **kwargs):
        self.mode = trade.TradingMode.PAPER
        self.bodies = []

    def place_order(self, body):  # pragma: no cover - must never be reached
        raise AssertionError("PAPER 런에서 주문이 전송됐습니다.")


def app_config(tmp_path, **trading):
    return AppConfig(
        toss=TossConfig(client_id="id", client_secret="secret"),
        notion=NotionConfig(token="", database_id=""),
        analyst=AnalystConfig(),
        trading=TradingConfig(
            enabled=True,
            kill_switch_path=str(tmp_path / "no-switch"),
            strategies=["tests.test_trade_cli:BuyOne"],
            **trading,
        ),
    )


@pytest.fixture
def wired(tmp_path, monkeypatch, firestore_client):
    config = app_config(tmp_path)
    monkeypatch.setattr(trade, "load_config", lambda: config)
    monkeypatch.setattr(trade, "PortfolioService", FakeService)
    monkeypatch.setattr(trade, "build_trading_api", lambda *a, **k: FakeTrading())
    # The clock the calendar is compared against - inside the KR session.
    monkeypatch.setattr(
        trade, "build_context", _pinned_context(trade.build_context)
    )
    return config


def _pinned_context(original):
    def build(service, store, **kwargs):
        kwargs["now"] = datetime(2026, 8, 26, 10, 0, tzinfo=KST)
        return original(service, store, **kwargs)

    return build


def test_a_paper_run_records_a_simulated_order(wired, firestore_client, capsys):
    assert trade.run([]) == trade.EXIT_OK

    store = Store(firestore_client)
    orders = store.recent_orders()
    assert len(orders) == 1
    assert (orders[0]["status"], orders[0]["mode"]) == ("simulated", "paper")

    signals = store.recent_signals()
    assert len(signals) == 1
    assert signals[0]["outcome"] == "accepted"
    assert signals[0]["reason"] == "테스트 신호"

    assert "PAPER" in capsys.readouterr().out


def test_a_rejected_signal_is_recorded_with_its_rule(
    tmp_path, monkeypatch, capsys, firestore_client
):
    config = app_config(tmp_path, limits={"max_orders_per_day": 0})
    monkeypatch.setattr(trade, "load_config", lambda: config)
    monkeypatch.setattr(trade, "PortfolioService", FakeService)
    monkeypatch.setattr(trade, "build_trading_api", lambda *a, **k: FakeTrading())
    monkeypatch.setattr(trade, "build_context", _pinned_context(trade.build_context))

    assert trade.run([]) == trade.EXIT_OK

    store = Store(firestore_client)
    assert store.recent_orders() == []
    row = store.recent_signals()[0]
    assert row["outcome"] == "rejected"
    assert row["reject_rule"] == "daily-order-limit"
    assert "daily-order-limit" in capsys.readouterr().out


def test_dry_run_writes_nothing(wired, firestore_client, capsys):
    assert trade.run(["--dry-run"]) == trade.EXIT_OK

    store = Store(firestore_client)
    assert store.recent_orders() == []
    assert store.recent_signals() == []
    assert "DRY-RUN" in capsys.readouterr().out


class FakeLiveOrders:
    """A LIVE TradingApi that accepts orders instead of refusing them."""

    mode = trade.TradingMode.LIVE

    def __init__(self):
        self.bodies = []

    def place_order(self, body):
        self.bodies.append(body)
        return {"orderId": "TOSS-LIVE-1"}


def test_live_sends_the_approved_order_for_real(wired, monkeypatch, firestore_client, capsys):
    live = FakeLiveOrders()
    asked = {}

    def build(config, mode=None, **kwargs):
        asked["mode"] = mode
        return live

    monkeypatch.setattr(trade, "build_trading_api", build)

    assert trade.run(["--live"]) == trade.EXIT_OK

    assert asked["mode"] is trade.TradingMode.LIVE
    assert len(live.bodies) == 1
    [order] = Store(firestore_client).recent_orders()
    assert (order["mode"], order["status"]) == ("live", "submitted")
    assert "실계좌 주문이 전송됩니다" in capsys.readouterr().out


def test_without_the_flag_nothing_is_sent(wired, monkeypatch):
    """Config cannot open LIVE; only the flag can."""
    asked = {}

    def build(config, mode=None, **kwargs):
        asked["mode"] = mode
        return FakeTrading()

    monkeypatch.setattr(trade, "build_trading_api", build)

    assert trade.run([]) == trade.EXIT_OK
    assert asked["mode"] is trade.TradingMode.PAPER


def test_disabled_trading_does_nothing(tmp_path, monkeypatch):
    config = app_config(tmp_path)
    config = type(config)(**{**config.__dict__, "trading": TradingConfig()})
    monkeypatch.setattr(trade, "load_config", lambda: config)
    assert trade.run([]) == trade.EXIT_DISABLED


# ---------------------------------------------------------- --reconcile (Phase 2 [9])


class FakeLiveTrading:
    """Stands in for a LIVE TradingApi during a --reconcile run."""

    mode = trade.TradingMode.LIVE

    def __init__(self):
        self.conditional_orders = []

    def get_order(self, order_id):
        return {"filledQuantity": "10", "avgFillPrice": "69950"}

    def list_orders(self, params=None):
        return []

    def place_conditional_order(self, body):
        self.conditional_orders.append(body)
        return {"conditionalOrderId": "COND-1"}


def test_reconcile_settles_a_live_order_and_arms_its_bracket(
    tmp_path, monkeypatch, capsys, firestore_client
):
    config = app_config(tmp_path)
    monkeypatch.setattr(trade, "load_config", lambda: config)
    fake_trading = FakeLiveTrading()
    monkeypatch.setattr(trade, "build_trading_api", lambda *a, **k: fake_trading)

    store = Store(firestore_client)
    store.client.collection("orders").document("entry-1").set(
        {
            "ts": "2026-08-27T09:00:00", "strategy": "s", "symbol": "005930",
            "side": "BUY", "order_type": "LIMIT", "quantity": "10",
            "amount": None, "price": "70000", "currency": "KRW",
            "notional_krw": "700000", "status": "submitted", "mode": "live",
            "order_id": "TOSS-1", "error_code": None,
            "stop_loss_price": "65000", "take_profit_price": "80000",
            "filled_quantity": "0", "oco_client_order_id": None,
            "oco_status": None, "updated_at": "2026-08-27T09:00:00",
        }
    )

    assert trade.run(["--reconcile"]) == trade.EXIT_OK

    order = store.order_by_client_id("entry-1")
    assert order["status"] == "filled"
    assert order["oco_status"] == "registered"
    assert len(fake_trading.conditional_orders) == 1

    out = capsys.readouterr().out
    assert "filled" in out
    assert "OCO 등록" in out


def test_reconcile_with_nothing_pending_says_so(tmp_path, monkeypatch, capsys):
    config = app_config(tmp_path)
    monkeypatch.setattr(trade, "load_config", lambda: config)
    monkeypatch.setattr(trade, "build_trading_api", lambda *a, **k: FakeLiveTrading())

    assert trade.run(["--reconcile"]) == trade.EXIT_OK
    assert "없습니다" in capsys.readouterr().out


def test_reconcile_does_not_touch_strategies_or_the_risk_gate(
    tmp_path, monkeypatch, capsys
):
    # --reconcile is a separate code path from the strategy-evaluation run;
    # loading a strategy must not be required to poll for fills.
    config = app_config(tmp_path)
    config = type(config)(
        **{**config.__dict__, "trading": TradingConfig(enabled=False)}
    )
    monkeypatch.setattr(trade, "load_config", lambda: config)
    monkeypatch.setattr(trade, "build_trading_api", lambda *a, **k: FakeLiveTrading())

    assert trade.run(["--reconcile"]) == trade.EXIT_OK
