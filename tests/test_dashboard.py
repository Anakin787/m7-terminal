"""Dashboard API against a stubbed Toss backend - no credentials, no network.

Also covers the cache, which is a correctness requirement rather than an
optimisation: the ACCOUNT rate limit group allows one request per second.
"""

import pytest
from fastapi.testclient import TestClient

from src.config import AnalystConfig, AppConfig, ManualHolding, NotionConfig, TossConfig
from src.dashboard import api as dashboard_api
from src.dashboard.service import DashboardService
from decimal import Decimal

HOLDINGS = {
    "totalPurchaseAmount": {"krw": "6500000", "usd": "1553"},
    "marketValue": {"amount": {"krw": "7200000", "usd": "1785"}},
    "profitLoss": {"amount": {"krw": "700000"}, "rate": "0.1179"},
    "items": [
        {
            "symbol": "005930", "name": "삼성전자", "marketCountry": "KR",
            "currency": "KRW", "quantity": "100", "lastPrice": "72000",
            "averagePurchasePrice": "65000",
            "marketValue": {"purchaseAmount": "6500000", "amount": "7200000",
                            "amountAfterCost": "7050000"},
            "profitLoss": {"amount": "700000", "amountAfterCost": "550000",
                           "rate": "0.1077", "rateAfterCost": "0.0846"},
            "dailyProfitLoss": {"amount": "100000", "rate": "0.0141"},
        }
    ],
}


class StubClient:
    """Counts calls so the cache can be asserted on."""

    def __init__(self):
        self.calls = []

    def get(self, path, *, group=None, params=None, account_seq=None):
        self.calls.append(path)
        if path == "/api/v1/accounts":
            return [{"accountNo": "12345678901", "accountSeq": 1, "accountType": "BROKERAGE"}]
        if path == "/api/v1/holdings":
            return HOLDINGS
        if path == "/api/v1/buying-power":
            currency = (params or {}).get("currency")
            return {"currency": currency,
                    "cashBuyingPower": "5000000" if currency == "KRW" else "3500.5"}
        if path == "/api/v1/exchange-rate":
            return {"baseCurrency": "USD", "quoteCurrency": "KRW", "rate": "1342.5"}
        if path == "/api/v1/prices":
            return [{"symbol": "AAPL", "lastPrice": "178.5", "currency": "USD"}]
        if path == "/api/v1/stocks":
            return [{"symbol": "AAPL", "name": "Apple Inc.",
                     "marketCountry": "US", "currency": "USD"}]
        if path.endswith("/warnings"):
            return {"volatilityInterruption": True}
        if path.startswith("/api/v1/market-calendar"):
            return {"sessions": []}
        raise AssertionError(f"unexpected path {path}")

    def close(self):
        pass


@pytest.fixture
def service(firestore_client):
    config = AppConfig(
        toss=TossConfig(client_id="cid", client_secret="sec"),
        notion=NotionConfig(token="secret_real", database_id="db"),
        analyst=AnalystConfig(api_key=None),
        manual_holdings=[
            ManualHolding(symbol="AAPL", qty=Decimal("10"), avg_price=Decimal("155.3"),
                          avg_exchange_rate=Decimal("1300"))
        ],
    )
    stub = StubClient()
    from src.pipeline import PortfolioService

    svc = DashboardService.__new__(DashboardService)
    svc.config = config
    from src.store.repo import Store

    svc.store = Store(firestore_client)
    svc.portfolio = PortfolioService(config, client=stub)
    from src.dashboard.service import _Cached, TTL_MARKET_STATUS, TTL_PORTFOLIO

    svc._snapshot = _Cached(TTL_PORTFOLIO)
    svc._status = _Cached(TTL_MARKET_STATUS)
    svc.last_sync = None
    svc.stub = stub
    yield svc
    svc.close()


@pytest.fixture
def client(service, monkeypatch):
    monkeypatch.setattr(dashboard_api, "_service", service)
    return TestClient(dashboard_api.app)


def test_overview_combines_both_sources(client):
    data = client.get("/api/overview").json()

    assert data["ready"]
    # 7,200,000 KRW + 1,785 USD * 1342.5
    assert data["total_krw"] == pytest.approx(7200000 + 1785 * 1342.5)
    assert data["exchange_rate"] == 1342.5
    assert data["buying_power"] == {"KRW": 5000000.0, "USD": 3500.5}


def test_after_cost_profit_is_exposed(client):
    data = client.get("/api/overview").json()

    assert data["profit_after_cost_krw"] == 550000.0
    assert data["profit_rate_after_cost"] is not None


def test_daily_profit_is_exposed(client):
    assert client.get("/api/overview").json()["daily_profit_krw"] == 100000.0


def test_manual_daily_pnl_flows_from_the_previous_close_fn(firestore_client):
    """A previous-close lookup, wired into PortfolioService, gives manual
    holdings a today's-P&L the Toss feed never carries for them."""
    from src.pipeline import PortfolioService

    config = AppConfig(
        toss=TossConfig(client_id="cid", client_secret="sec"),
        notion=NotionConfig(token="secret_real", database_id="db"),
        analyst=AnalystConfig(api_key=None),
        manual_holdings=[
            ManualHolding(symbol="AAPL", qty=Decimal("10"), avg_price=Decimal("155.3"),
                          avg_exchange_rate=Decimal("1300"))
        ],
    )
    stub = StubClient()
    # AAPL live quote is 178.5 (StubClient); previous close 170 -> +8.5/share.
    svc = PortfolioService(
        config, client=stub, previous_close_fn=lambda symbols: {"AAPL": Decimal("170")}
    )
    snapshot = svc.snapshot()
    aapl = next(p for p in snapshot.positions if p.symbol == "AAPL")

    assert aapl.daily_profit_loss == Decimal("85.0")
    # KRW daily total = Toss item 005930 (+100,000 KRW) + AAPL (85 USD * 1342.5).
    assert snapshot.daily_profit_krw == pytest.approx(
        Decimal("100000") + Decimal("85.0") * Decimal("1342.5")
    )


def test_previous_close_fn_failure_does_not_break_the_snapshot(firestore_client):
    from src.pipeline import PortfolioService

    config = AppConfig(
        toss=TossConfig(client_id="cid", client_secret="sec"),
        notion=NotionConfig(token="secret_real", database_id="db"),
        analyst=AnalystConfig(api_key=None),
        manual_holdings=[
            ManualHolding(symbol="AAPL", qty=Decimal("10"), avg_price=Decimal("155.3"))
        ],
    )

    def boom(symbols):
        raise RuntimeError("yfinance unreachable")

    svc = PortfolioService(config, client=StubClient(), previous_close_fn=boom)
    snapshot = svc.snapshot()
    aapl = next(p for p in snapshot.positions if p.symbol == "AAPL")

    assert aapl.daily_profit_loss is None


def test_warnings_are_surfaced(client):
    warnings = client.get("/api/overview").json()["warnings"]

    assert any("VI" in w for w in warnings)
    assert any("삼성전자" in w for w in warnings)


def test_holdings_carry_source_badges_and_weights(client):
    positions = client.get("/api/holdings").json()["positions"]

    by_symbol = {p["symbol"]: p for p in positions}
    assert by_symbol["005930"]["source"] == "toss"
    assert by_symbol["AAPL"]["source"] == "manual"
    assert sum(p["weight"] for p in positions) == pytest.approx(1.0)


def test_allocation_by_market_and_currency(client):
    market = client.get("/api/allocation?by=market").json()
    currency = client.get("/api/allocation?by=currency").json()

    assert {s["key"] for s in market["segments"]} == {"KR", "US"}
    assert {s["key"] for s in currency["segments"]} == {"KRW", "USD"}
    assert sum(s["share"] for s in market["segments"]) == pytest.approx(1.0)


def test_history_is_empty_before_any_report_run(client):
    data = client.get("/api/history?range=3M").json()

    assert data["points"] == []
    assert data["total_snapshots"] == 0


def test_history_returns_saved_snapshots(client, service):
    snapshot, _ = service.snapshot()
    service.store.save_snapshot(snapshot, ts="2026-08-19T10:00:00")

    data = client.get("/api/history?range=ALL").json()

    assert len(data["points"]) == 1
    assert data["points"][0]["ts"] == "2026-08-19T10:00:00"


def test_invalid_range_is_rejected(client):
    assert client.get("/api/history?range=99Y").status_code == 422


def test_cache_prevents_hammering_the_account_endpoint(client, service):
    """Several tabs polling must not multiply upstream calls - ACCOUNT is 1 TPS."""
    for _ in range(5):
        client.get("/api/overview")
        client.get("/api/holdings")

    assert service.stub.calls.count("/api/v1/holdings") == 1


def test_settings_masks_credentials(client):
    data = client.get("/api/settings").json()

    assert "sec" not in data["toss"]["client_secret"] or "..." in data["toss"]["client_secret"]
    assert data["toss"]["client_secret"] != "sec"


def test_health_reports_trading_disabled_in_phase_1(client):
    data = client.get("/api/health").json()

    assert data["connected"] is True
    assert data["trading_enabled"] is False


def test_index_page_is_served(client):
    response = client.get("/")

    assert response.status_code == 200
    assert "M7 Terminal" in response.text


def test_holdings_expose_cost_basis_and_krw_profit(client):
    """The invested amount must be visible next to the current value."""
    positions = {p["symbol"]: p for p in client.get("/api/holdings").json()["positions"]}

    krw = positions["005930"]
    assert krw["cost_krw"] == 6500000.0
    assert krw["value_krw"] == 7200000.0
    assert krw["profit_krw"] == pytest.approx(700000.0)

    # The USD position converts its cost at the purchase-time rate (1300),
    # not today's, so cost_krw reflects what was actually spent.
    usd = positions["AAPL"]
    assert usd["cost_krw"] == pytest.approx(1553 * 1300)


def test_holdings_expose_fx_pnl_for_foreign_positions_with_a_purchase_rate(client):
    positions = {p["symbol"]: p for p in client.get("/api/holdings").json()["positions"]}

    # KRW position - no currency exposure, no FX P&L.
    assert positions["005930"]["fx_pnl_krw"] is None

    # AAPL: bought at 1300, today's rate is 1342.5 - the won weakened, so the
    # unchanged 1553 USD cost basis is worth more in KRW now.
    assert positions["AAPL"]["fx_pnl_krw"] == pytest.approx(1553 * (1342.5 - 1300))


def test_overview_totals_the_fx_pnl_across_positions(client):
    overview = client.get("/api/overview").json()
    assert overview["fx_pnl_krw"] == pytest.approx(1553 * (1342.5 - 1300))


def test_position_costs_sum_to_the_portfolio_total(client):
    data = client.get("/api/holdings").json()["positions"]
    overview = client.get("/api/overview").json()

    assert sum(p["cost_krw"] for p in data) == pytest.approx(overview["purchase_krw"])
    assert sum(p["value_krw"] for p in data) == pytest.approx(overview["total_krw"])


def test_rename_persists_and_shows_up_immediately(client):
    """Renaming must bypass the 60s cache, or the edit looks like it failed."""
    assert client.get("/api/holdings").json()["positions"][0]["name"] == "삼성전자"

    response = client.put("/api/holdings/005930/name", json={"name": "삼성전자 (메인)"})
    assert response.status_code == 200
    assert response.json()["name"] == "삼성전자 (메인)"

    names = {p["symbol"]: p["name"] for p in client.get("/api/holdings").json()["positions"]}
    assert names["005930"] == "삼성전자 (메인)"


def test_blank_name_clears_the_override(client):
    client.put("/api/holdings/AAPL/name", json={"name": "애플"})
    assert client.put("/api/holdings/AAPL/name", json={"name": "  "}).json()["name"] is None

    names = {p["symbol"]: p["name"] for p in client.get("/api/holdings").json()["positions"]}
    assert names["AAPL"] == "Apple Inc."   # back to the API-supplied name


def test_rename_rejects_a_malformed_symbol(client):
    assert client.put("/api/holdings/..%2Fetc/name", json={"name": "x"}).status_code in (404, 422)


def test_rename_rejects_an_overlong_name(client):
    assert client.put("/api/holdings/005930/name",
                      json={"name": "x" * 200}).status_code == 422


def test_overrides_survive_a_new_service_on_the_same_db(service, firestore_client):
    """The Notion report reads the same overrides, so they must be in Firestore."""
    service.store.set_symbol_name("005930", "삼성전자 (메인)")

    from src.store.repo import Store

    assert Store(firestore_client).symbol_names()["005930"] == "삼성전자 (메인)"


def test_audit_endpoint_returns_entries_newest_first(client, service):
    service.store.save_audit_entries(
        [
            {"detected_at": "2026-08-26T09:00:00", "category": "universe",
             "actor_kind": "human", "actor": "jiun@box", "summary": "추가 1종목 (MSFT)",
             "changes": [{"target": "MSFT", "before": None, "after": "추가"}]},
            {"detected_at": "2026-08-27T09:00:00", "category": "veto",
             "actor_kind": "ai", "actor": "AI universe review", "summary": "보류: INTC",
             "changes": [{"target": "INTC", "before": None, "after": "[trading_halt] 정지"}]},
        ]
    )

    entries = client.get("/api/audit").json()["entries"]

    assert [e["category"] for e in entries] == ["veto", "universe"]
    assert entries[0]["actor_kind"] == "ai"


def _signal(**overrides):
    from src.strategy.base import ORDER_MARKET, SIDE_BUY, Signal

    # An amount order, which is what bucket-dca buys with - no quantity and
    # no limit price until it fills.
    base = dict(strategy="bucket-dca", symbol="QQQ", side=SIDE_BUY,
                order_type=ORDER_MARKET, reason="WEEKLY 배분 — CORE 버킷",
                amount=Decimal("484.31"), currency="USD")
    base.update(overrides)
    return Signal(**base)


def test_activity_endpoint_returns_signals_and_orders_separately(client, service):
    """Rejected signals have no order, and the endpoint must still show them.

    Joining the two would have to either drop this row or invent an order to
    hang it on - and "the gate refused everything" is precisely what someone
    opens this page to find out.
    """
    from src.execution.risk import Rejection, RiskDecision

    service.store.save_decision(RiskDecision(signal=_signal(), intent=object()))
    service.store.save_decision(
        RiskDecision(signal=_signal(symbol="SHY"),
                     rejection=Rejection("order-hours-closed", "정규장이 아닙니다")),
    )

    data = client.get("/api/trading/activity").json()

    outcomes = {row["symbol"]: row["outcome"] for row in data["signals"]}
    assert outcomes == {"QQQ": "accepted", "SHY": "rejected"}
    rejected = next(r for r in data["signals"] if r["symbol"] == "SHY")
    assert rejected["reject_rule"] == "order-hours-closed"
    assert rejected["reject_detail"] == "정규장이 아닙니다"
    assert data["orders"] == []


def test_activity_endpoint_reports_the_mode_each_order_ran_in(client, service):
    """Design section 7 calls PAPER/LIVE confusion a live risk.

    A table of orders is where it would bite, so the mode travels with every
    row rather than being inferred from the page's engine badge.
    """
    from src.execution.risk import OrderIntent

    intent = OrderIntent(
        signal=_signal(), symbol="QQQ", side="BUY", order_type="MARKET",
        currency="USD", amount=Decimal("484.31"), notional_krw=Decimal("650000"),
    )
    service.store.save_order(intent.with_client_order_id("bucket_dca-QQQ-2026-09-09-1"),
                             status="simulated", mode="paper")

    orders = client.get("/api/trading/activity").json()["orders"]

    assert len(orders) == 1
    assert orders[0]["client_order_id"] == "bucket_dca-QQQ-2026-09-09-1"
    assert (orders[0]["status"], orders[0]["mode"]) == ("simulated", "paper")
    # Amount orders carry no quantity until they fill; the column must not
    # silently become blank for every buy this strategy makes.
    assert orders[0]["amount"] == "484.31"


def test_activity_endpoint_is_empty_before_anything_has_run(client):
    assert client.get("/api/trading/activity").json() == {"signals": [], "orders": []}


def test_audit_endpoint_filters_by_category(client, service):
    service.store.save_audit_entries(
        [
            {"detected_at": "2026-08-26T09:00:00", "category": "universe", "summary": "u"},
            {"detected_at": "2026-08-27T09:00:00", "category": "limits", "summary": "l"},
        ]
    )

    entries = client.get("/api/audit?category=limits").json()["entries"]

    assert [e["summary"] for e in entries] == ["l"]


def test_audit_endpoint_rejects_an_unknown_category(client):
    # The pattern is the allow-list; an arbitrary category would silently
    # return an empty list and read as "nothing ever happened".
    assert client.get("/api/audit?category=whatever").status_code == 422


# ------------------------------------------------------------- kill switch


@pytest.fixture
def halted_paths(service, tmp_path):
    """Point the service's kill switch at a temp file, not the repo root."""
    import dataclasses

    from src.config import TradingConfig

    path = tmp_path / "KILL_SWITCH"
    service.config = dataclasses.replace(
        service.config,
        trading=TradingConfig(
            enabled=True,
            kill_switch_path=str(path),
            strategies=["src.strategy.momentum_dca:MomentumDCA"],
        ),
    )
    return path


def test_status_reports_the_three_facts_separately(client, halted_paths):
    data = client.get("/api/trading/status").json()

    assert data["engine_enabled"] is True
    assert data["kill_switch"]["active"] is False
    assert data["halted"] is False
    # LIVE stays shut until design 6 [10] has its own verification.
    assert data["live_open"] is False
    assert data["mode"] == "paper"


def test_engaging_the_kill_switch_creates_the_file_the_gate_reads(client, halted_paths):
    response = client.post(
        "/api/trading/kill-switch", json={"active": True, "reason": "장중 이상"}
    )

    assert response.status_code == 200
    assert response.json()["halted"] is True
    assert halted_paths.exists()

    from src.execution.risk import kill_switch_active

    assert kill_switch_active(str(halted_paths)) is True


def test_a_flip_is_audited_and_a_repeat_is_not(client, halted_paths, service):
    client.post("/api/trading/kill-switch", json={"active": True, "reason": "장중 이상"})
    client.post("/api/trading/kill-switch", json={"active": True, "reason": "장중 이상"})
    client.post("/api/trading/kill-switch", json={"active": False})

    entries = client.get("/api/audit?category=kill_switch").json()["entries"]

    # Two transitions, three requests: re-engaging an engaged switch changed
    # nothing, and a log full of non-events is unreadable on the day it counts.
    assert len(entries) == 2
    assert entries[0]["changes"][0]["after"] == "해제"
    assert "장중 이상" in entries[1]["summary"]
    assert entries[1]["changed_by_method"] == "direct"


def test_releasing_a_switch_that_is_not_engaged_is_harmless(client, halted_paths):
    data = client.post("/api/trading/kill-switch", json={"active": False}).json()

    assert data["halted"] is False
    assert client.get("/api/audit?category=kill_switch").json()["entries"] == []


# -------------------------------------------------------------- freshness


def test_health_reports_a_fresh_snapshot_as_fresh(client, service):
    service.store.save_snapshot(service.portfolio.snapshot())

    data = client.get("/api/health").json()

    assert data["snapshot_stale"] is False
    assert data["snapshot_age_hours"] < 1
    assert data["last_snapshot_ts"] is not None


def test_health_flags_a_snapshot_older_than_a_day(client, service):
    from datetime import datetime, timedelta

    stale = (datetime.now() - timedelta(days=3)).isoformat()
    service.store.save_snapshot(service.portfolio.snapshot(), ts=stale)

    data = client.get("/api/health").json()

    # The August outage was invisible because a job that does not run leaves
    # no error - only an ageing newest row.
    assert data["snapshot_stale"] is True
    assert data["snapshot_age_hours"] > 70


def test_no_snapshots_at_all_is_not_reported_as_stale(client):
    data = client.get("/api/health").json()

    # Never having run and having stopped running are different problems, and
    # snapshot_count already says which one this is.
    assert data["snapshot_stale"] is False
    assert data["last_snapshot_ts"] is None
    assert data["snapshot_count"] == 0


@pytest.fixture
def bucket_plan(service):
    """Give the service an active strategy that actually has a bucket plan.

    ``_bucket_allocation`` reads its targets from the loaded strategy, so with
    the default (empty) TradingConfig the endpoint correctly answers "활성
    전략에 버킷 계획이 없습니다" and returns no segments. Asserting on segment
    order therefore needs a strategy registered, not just a portfolio.
    """
    import dataclasses

    from src.config import TradingConfig

    service.config = dataclasses.replace(
        service.config,
        trading=TradingConfig(
            enabled=True,
            strategies=["src.strategy.bucket_dca:BucketDcaStrategy"],
            # Loading a strategy fails outright if the gate's ceiling sits
            # below any instrument's target weight, and the highest in
            # DEFAULT_UNIVERSE is QQQ at 0.60. Without this the load raises,
            # the endpoint's catch-all turns it into "no bucket plan", and
            # the test below reads as a missing feature rather than a
            # misconfigured fixture.
            #
            # 0.65, not 0.60: config values arrive as floats and are compared
            # against Decimal target weights, so an exactly-equal ceiling is
            # read as exceeded (float 0.6 < Decimal("0.60")). config.yaml
            # pads its overrides for the same reason.
            limits={"max_position_weight": 0.65},
        ),
    )
    return service


def test_allocation_by_bucket_says_so_when_no_strategy_plans_buckets(client):
    body = client.get("/api/allocation?by=bucket").json()

    # A chart with nothing to plot must not look like a chart with zeroes.
    assert body["segments"] == []
    assert "버킷 계획" in body["error"]


def test_allocation_by_bucket_reports_target_and_gap(bucket_plan, client):
    response = client.get("/api/allocation?by=bucket")
    assert response.status_code == 200
    body = response.json()
    assert body["by"] == "bucket"
    keys = [s["key"] for s in body["segments"]]
    # Safe first, unmanaged last - a risk ladder, then the money no plan covers.
    assert keys[0] == "SAFE"
    if "UNMANAGED" in keys:
        assert keys[-1] == "UNMANAGED"
        unmanaged = next(s for s in body["segments"] if s["key"] == "UNMANAGED")
        assert unmanaged["target"] is None


def test_allocation_rejects_a_grouping_it_does_not_know(client):
    assert client.get("/api/allocation?by=sector").status_code == 422
