"""Fill detection, and arming an OCO bracket for a newly-filled BUY."""

from decimal import Decimal

import pytest

from src.execution.reconciler import Reconciler
from src.store.repo import Store
from src.toss.errors import TossApiError
from src.toss.trading import TradingMode


class FakeTrading:
    def __init__(self, mode=TradingMode.LIVE, order_responses=None, history=None):
        self.mode = mode
        self.order_responses = dict(order_responses or {})
        self.history = list(history or [])
        self.conditional_orders = []

    def get_order(self, order_id):
        response = self.order_responses.get(order_id)
        if isinstance(response, Exception):
            raise response
        if response is None:
            raise TossApiError(404, "order-not-found", "no such order")
        return response

    def list_orders(self, params=None):
        return self.history

    def place_conditional_order(self, body):
        self.conditional_orders.append(body)
        outcome = getattr(self, "next_conditional_outcome", None)
        if isinstance(outcome, Exception):
            raise outcome
        return {"conditionalOrderId": "COND-1"}


def store(tmp_path=None):
    return Store()


def seed_order(db, client_order_id="s-005930-2026-08-27-1", **overrides):
    fields = dict(
        client_order_id=client_order_id,
        signal_id=None,
        ts="2026-08-27T09:00:00",
        strategy="s",
        symbol="005930",
        side="BUY",
        order_type="LIMIT",
        quantity="10",
        amount=None,
        price="70000",
        currency="KRW",
        notional_krw="700000",
        status="submitted",
        mode="live",
        order_id="TOSS-1",
        error_code=None,
        stop_loss_price=None,
        take_profit_price=None,
        filled_quantity="0",
        oco_client_order_id=None,
        oco_status=None,
        updated_at="2026-08-27T09:00:00",
    )
    fields.update(overrides)
    fields.pop("client_order_id")
    db.client.collection("orders").document(client_order_id).set(fields)
    return client_order_id


def reconciler(trading, db, **kwargs):
    return Reconciler(trading, db, **kwargs)


# --------------------------------------------------------------- construction


def test_reconciler_refuses_a_paper_trading_api(firestore_client):
    with pytest.raises(ValueError):
        Reconciler(FakeTrading(mode=TradingMode.PAPER), Store(firestore_client))


# -------------------------------------------------------------- fill detection


def test_a_fully_filled_order_is_marked_filled_and_records_one_fill(firestore_client):
    db = Store(firestore_client)
    cid = seed_order(db, quantity="10")
    trading = FakeTrading(
        order_responses={"TOSS-1": {"filledQuantity": "10", "avgFillPrice": "69950"}}
    )

    reconciler(trading, db).run()

    order = db.order_by_client_id(cid)
    assert order["status"] == "filled"
    assert order["filled_quantity"] == "10"

    fills = db.fills_for_order("TOSS-1")
    assert len(fills) == 1
    assert fills[0]["quantity"] == "10"
    assert fills[0]["price"] == "69950"


def test_a_partial_fill_is_recorded_without_closing_the_order(firestore_client):
    db = Store(firestore_client)
    cid = seed_order(db, quantity="10")
    trading = FakeTrading(order_responses={"TOSS-1": {"filledQuantity": "4"}})

    reconciler(trading, db).run()

    order = db.order_by_client_id(cid)
    assert order["status"] == "partially_filled"
    assert order["filled_quantity"] == "4"


def test_a_second_pass_only_records_the_new_fill_amount(firestore_client):
    # The broker reports cumulative filled quantity, not a per-call delta -
    # recording the raw number twice would double-count the fill.
    db = Store(firestore_client)
    cid = seed_order(db, quantity="10")
    trading = FakeTrading(order_responses={"TOSS-1": {"filledQuantity": "4"}})
    reconciler(trading, db).run()

    trading.order_responses["TOSS-1"] = {"filledQuantity": "10"}
    reconciler(trading, db).run()

    fills = db.fills_for_order("TOSS-1")
    assert sorted(f["quantity"] for f in fills) == ["4", "6"]
    assert db.order_by_client_id(cid)["status"] == "filled"


def test_no_new_fill_is_a_no_op(firestore_client):
    db = Store(firestore_client)
    seed_order(db, quantity="10")
    trading = FakeTrading(order_responses={"TOSS-1": {"filledQuantity": "0"}})

    reconciler(trading, db).run()

    assert db.fills_for_order("TOSS-1") == []


def test_cancellation_is_read_from_the_status_string_when_no_shares_filled(firestore_client):
    db = Store(firestore_client)
    cid = seed_order(db)
    trading = FakeTrading(order_responses={"TOSS-1": {"status": "CANCELLED"}})

    reconciler(trading, db).run()

    assert db.order_by_client_id(cid)["status"] == "canceled"


def test_an_order_with_no_order_id_is_found_by_scanning_history(firestore_client):
    # This is the "unknown"-status path: the executor never received an
    # orderId, so the only way back to this order is its clientOrderId.
    db = Store(firestore_client)
    cid = seed_order(db, order_id=None, status="unknown")
    trading = FakeTrading(
        history=[{"clientOrderId": cid, "orderId": "TOSS-9", "filledQuantity": "10"}]
    )

    reconciler(trading, db).run()

    order = db.order_by_client_id(cid)
    assert order["order_id"] == "TOSS-9"
    assert order["status"] == "filled"


def test_an_unreadable_order_is_left_alone_for_the_next_pass(firestore_client):
    db = Store(firestore_client)
    cid = seed_order(db)
    trading = FakeTrading(
        order_responses={"TOSS-1": TossApiError(500, "boom", "서버 오류")}
    )

    reconciler(trading, db).run()

    order = db.order_by_client_id(cid)
    assert order["status"] == "submitted"  # unchanged, not silently marked failed


# -------------------------------------------------------------------- OCO


def test_a_filled_buy_with_a_bracket_arms_an_oco(firestore_client):
    db = Store(firestore_client)
    cid = seed_order(
        db, quantity="10", stop_loss_price="65000", take_profit_price="80000"
    )
    trading = FakeTrading(order_responses={"TOSS-1": {"filledQuantity": "10"}})

    results = reconciler(trading, db).run()

    assert len(trading.conditional_orders) == 1
    body = trading.conditional_orders[0]
    assert body["quantity"] == "10"
    assert body["symbol"] == "005930"

    order = db.order_by_client_id(cid)
    assert order["oco_status"] == "registered"
    assert order["oco_client_order_id"] == f"oco-{cid}"
    assert "OCO 등록" in results[0]

    cond = db.conditional_order_by_client_id(f"oco-{cid}")
    assert cond["status"] == "registered"
    assert cond["entry_client_order_id"] == cid


def test_a_fill_with_no_bracket_prices_registers_nothing(firestore_client):
    db = Store(firestore_client)
    seed_order(db, quantity="10")  # no stop/take-profit
    trading = FakeTrading(order_responses={"TOSS-1": {"filledQuantity": "10"}})

    reconciler(trading, db).run()

    assert trading.conditional_orders == []


def test_a_sell_fill_never_arms_a_bracket(firestore_client):
    db = Store(firestore_client)
    seed_order(
        db, side="SELL", quantity="10",
        stop_loss_price="65000", take_profit_price="80000",
    )
    trading = FakeTrading(order_responses={"TOSS-1": {"filledQuantity": "10"}})

    reconciler(trading, db).run()

    assert trading.conditional_orders == []


def test_a_partial_fill_arms_the_bracket_for_the_filled_amount_so_far(firestore_client):
    db = Store(firestore_client)
    seed_order(
        db, quantity="10", stop_loss_price="65000", take_profit_price="80000",
    )
    trading = FakeTrading(order_responses={"TOSS-1": {"filledQuantity": "4"}})

    reconciler(trading, db).run()

    assert trading.conditional_orders[0]["quantity"] == "4"


def test_the_bracket_is_only_armed_once_across_reconcile_passes(firestore_client):
    db = Store(firestore_client)
    cid = seed_order(
        db, quantity="10", stop_loss_price="65000", take_profit_price="80000",
    )
    trading = FakeTrading(order_responses={"TOSS-1": {"filledQuantity": "10"}})
    reconciler(trading, db).run()
    assert len(trading.conditional_orders) == 1

    # A later pass still finds a "filled" order (nothing new to fill), and
    # must not re-register the bracket it already placed.
    reconciler(trading, db).run()
    assert len(trading.conditional_orders) == 1


def test_a_failed_oco_registration_does_not_stop_the_run_or_lose_the_fill(firestore_client):
    db = Store(firestore_client)
    cid = seed_order(
        db, quantity="10", stop_loss_price="65000", take_profit_price="80000",
    )
    trading = FakeTrading(order_responses={"TOSS-1": {"filledQuantity": "10"}})
    trading.next_conditional_outcome = TossApiError(
        422, "duplicate-conditional-order", "이미 등록됨"
    )

    results = reconciler(trading, db).run()

    order = db.order_by_client_id(cid)
    assert order["status"] == "filled"  # the fill itself is not lost
    assert order["oco_status"] == "failed"
    assert "OCO 등록 실패" in results[0]

    cond = db.conditional_order_by_client_id(f"oco-{cid}")
    assert cond["status"] == "failed"
    assert cond["error_code"] == "duplicate-conditional-order"


def test_the_oco_expiry_uses_the_configured_number_of_days(firestore_client):
    from datetime import datetime

    db = Store(firestore_client)
    seed_order(db, quantity="10", stop_loss_price="65000", take_profit_price="80000")
    trading = FakeTrading(order_responses={"TOSS-1": {"filledQuantity": "10"}})

    reconciler(
        trading, db, oco_expire_days=7, clock=lambda: datetime(2026, 8, 27)
    ).run()

    assert trading.conditional_orders[0]["expireDate"] == "2026-09-03"


def test_pending_orders_ignores_paper_and_terminal_orders(firestore_client):
    db = Store(firestore_client)
    seed_order(db, client_order_id="live-open", mode="live", status="submitted")
    seed_order(db, client_order_id="live-partial", mode="live", status="partially_filled")
    seed_order(db, client_order_id="live-done", mode="live", status="filled")
    seed_order(db, client_order_id="paper-open", mode="paper", status="simulated")

    pending = {o["client_order_id"] for o in db.pending_orders(mode="live")}
    assert pending == {"live-open", "live-partial"}


# ------------------------------------------------------- amount orders settle
#
# bucket-dca buys are amount-denominated ("$50.98 of SHY"), so the stored
# quantity is None and there is no share count for the fill to be measured
# against. Every order the active strategy places is one of these, and they
# used to sit at partially_filled for good - re-polled every thirty minutes,
# never shown as filled - however plainly the broker said otherwise.


def seed_amount_order(db, **overrides):
    fields = dict(
        quantity=None, amount="50.98", price=None, currency="USD",
        symbol="SHY", notional_krw="68356", order_type="MARKET",
    )
    fields.update(overrides)
    return seed_order(db, client_order_id="bucket_dca-SHY-2026-09-08-1", **fields)


def test_an_amount_order_settles_on_a_zero_remaining_quantity(firestore_client):
    db = Store(firestore_client)
    cid = seed_amount_order(db)
    trading = FakeTrading(
        order_responses={
            "TOSS-1": {
                "filledQuantity": "3",
                "remainingQuantity": "0",
                "avgFillPrice": "16.99",
            }
        }
    )

    reconciler(trading, db).run()

    order = db.order_by_client_id(cid)
    assert order["status"] == "filled"
    assert order["filled_quantity"] == "3"
    assert db.fills_for_order("TOSS-1")[0]["price"] == "16.99"


def test_an_amount_order_settles_on_the_status_when_no_quantity_remains(firestore_client):
    db = Store(firestore_client)
    cid = seed_amount_order(db)
    trading = FakeTrading(
        order_responses={"TOSS-1": {"filledQuantity": "3", "status": "FILLED"}}
    )

    reconciler(trading, db).run()

    assert db.order_by_client_id(cid)["status"] == "filled"


def test_an_amount_order_with_quantity_left_stays_partial(firestore_client):
    db = Store(firestore_client)
    cid = seed_amount_order(db)
    trading = FakeTrading(
        order_responses={"TOSS-1": {"filledQuantity": "3", "remainingQuantity": "1"}}
    )

    reconciler(trading, db).run()

    assert db.order_by_client_id(cid)["status"] == "partially_filled"


@pytest.mark.parametrize("status", ["부분체결", "UNFILLED", "PARTIALLY_FILLED", "OPEN"])
def test_a_status_that_merely_contains_filled_does_not_settle(firestore_client, status):
    """"unfilled" contains "filled"; "미체결" contains "체결"."""
    db = Store(firestore_client)
    cid = seed_amount_order(db)
    trading = FakeTrading(
        order_responses={"TOSS-1": {"filledQuantity": "3", "status": status}}
    )

    reconciler(trading, db).run()

    assert db.order_by_client_id(cid)["status"] == "partially_filled"


def test_an_amount_order_with_no_evidence_either_way_stays_open(firestore_client):
    """Never declare a fill nobody reported - the order keeps being polled."""
    db = Store(firestore_client)
    cid = seed_amount_order(db)
    trading = FakeTrading(order_responses={"TOSS-1": {"filledQuantity": "3"}})

    reconciler(trading, db).run()

    order = db.order_by_client_id(cid)
    assert order["status"] == "partially_filled"
    assert cid in [o["client_order_id"] for o in db.pending_orders()]


# ---------------------------------------------- unreadable versus not present


def test_a_failed_lookup_is_retried_next_pass(firestore_client):
    db = Store(firestore_client)
    seed_order(db)
    trading = FakeTrading(
        order_responses={"TOSS-1": TossApiError(503, "server-error", "down")}
    )

    [result] = reconciler(trading, db).run()

    assert "조회 실패" in result


def test_an_order_absent_from_the_history_says_so_instead(firestore_client):
    """The path an `unknown` order takes when the broker never got it.

    Distinct from a failed lookup: the history was read, and this order is
    not in it. Not marked failed either - that would assert no shares were
    bought, from a list whose query shape this project cannot confirm.
    """
    db = Store(firestore_client)
    cid = seed_order(db, status="unknown", order_id=None)
    trading = FakeTrading(history=[{"clientOrderId": "someone-else-1"}])

    [result] = reconciler(trading, db).run()

    assert "브로커 주문 이력에 없습니다" in result
    assert db.order_by_client_id(cid)["status"] == "unknown"
