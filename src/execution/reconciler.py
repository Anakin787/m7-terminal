"""Settling submitted orders against the broker, and arming the exit bracket.

This is what closes the loop the executor leaves open. Submitting an order
answers "did the request go through"; it says nothing about "did it fill,
and for how much" - and an ``unknown`` order from an ambiguous in-flight
response answers neither. The reconciler is the only thing that polls to
find out (design 3.1: "Reconciler <- GET /orders").

Once a BUY order's fill is confirmed, and the signal that produced it named
a stop-loss or take-profit, the reconciler places the OCO bracket for the
filled quantity. This is deliberately not the executor's job: an OCO belongs
against filled shares, not against an order that might still be pending,
partially filled, or rejected. Placing it here, one step later, is what
keeps "did we actually get the shares" and "are they now protected" from
being conflated into one race.

A signal that names *neither* leg gets no bracket - ``_arm_bracket`` returns
early. As of this writing the only wired-up strategy, ``momentum-dca``,
emits no stop/take-profit on its buys by design (see its module docstring:
the stop distance is left for step 8's backtest to set), so in practice no
bracket is armed for any live buy yet. This code path is exercised only once
a strategy starts populating those fields. Do not read "the reconciler arms
OCO brackets" as "live buys are protected by a stop" - today they are not.

PAPER orders are skipped entirely. A PAPER order was never sent, so there is
nothing on the broker's side to poll; simulating a fill would mean
inventing execution behaviour that belongs to the backtest engine, not here.
Real settlement - and therefore a real OCO - only happens once LIVE opens.
"""

from datetime import datetime, timedelta
from decimal import Decimal

from src.models import to_decimal
from src.toss.errors import TossApiError
from src.toss.trading import TradingMode, conditional_order_body

STATUS_FILLED = "filled"
STATUS_PARTIALLY_FILLED = "partially_filled"
STATUS_CANCELED = "canceled"
STATUS_REJECTED = "rejected"

#: Substrings looked for, case-insensitively, in whatever status string the
#: broker returns - see _infer_status for why this is a fallback rather than
#: the primary signal.
_CANCEL_HINTS = ("cancel", "취소")
_REJECT_HINTS = ("reject", "거부", "실패")

#: Read *before* the positive hints below, because several of these contain
#: one as a substring: "unfilled" contains "filled", "미체결" contains "체결".
#: Checking the other order would read every one of them as complete.
_NOT_COMPLETE_HINTS = ("partial", "부분", "unfilled", "미체결", "pending", "open")
_COMPLETE_HINTS = ("filled", "executed", "complete", "done", "체결완료", "완료")

#: Field name candidates the broker's order/history payload might use.
#: Unconfirmed - design section 2 documents the request shapes, not this
#: response - so every reader here scans several spellings rather than
#: committing to one, the same defensive style _pick already uses in
#: src/execution/context.py.
_FILLED_QTY_KEYS = ("filledQuantity", "executedQuantity", "cumulativeQuantity")
_AVG_PRICE_KEYS = ("avgFillPrice", "averagePrice", "executedPrice")
_COMMISSION_KEYS = ("commission", "fee")
_TAX_KEYS = ("tax",)
_STATUS_KEYS = ("status", "orderStatus")
_REMAINING_QTY_KEYS = (
    "remainingQuantity", "unfilledQuantity", "restQuantity", "openQuantity",
)
_ORDER_ID_KEYS = ("orderId", "id")
_CLIENT_ID_KEYS = ("clientOrderId", "client_order_id")


#: Returned by _fetch when the broker could not be asked at all, as distinct
#: from None, which is the broker answering that it has no such order. They
#: call for opposite things - retry the question, versus stop asking it - and
#: conflating them reported a missing order as a failed lookup forever.
_UNREADABLE = object()


def _pick(item, keys):
    if not isinstance(item, dict):
        return None
    for key in keys:
        if item.get(key) is not None:
            return item.get(key)
    return None


def _pick_decimal(item, keys):
    value = _pick(item, keys)
    return to_decimal(value)


def _complete_without_a_target(payload):
    """Is an *amount* order finished? Its ordered quantity was never a number.

    "$50.98 of SHY" names no share count, so there is nothing for the filled
    quantity to be compared against - and comparing it to None used to mean
    every amount order stayed ``partially_filled`` for good, however plainly
    the broker said otherwise. Every buy the active strategy places is an
    amount order, so that was all of them.

    A remaining quantity is preferred over the status string for the same
    reason the caller prefers quantities generally: it is a number, and the
    status enum's spelling is not confirmed anywhere this project has a
    source for. With neither available the answer stays "partially", which
    keeps an order open rather than declaring a fill nobody reported.
    """
    remaining = _pick_decimal(payload, _REMAINING_QTY_KEYS)
    if remaining is not None:
        return STATUS_FILLED if remaining <= 0 else STATUS_PARTIALLY_FILLED

    raw_status = str(_pick(payload, _STATUS_KEYS) or "").lower()
    if any(hint in raw_status for hint in _NOT_COMPLETE_HINTS):
        return STATUS_PARTIALLY_FILLED
    if any(hint in raw_status for hint in _COMPLETE_HINTS):
        return STATUS_FILLED
    return STATUS_PARTIALLY_FILLED


def _infer_status(payload, filled_qty, ordered_qty):
    """Fill state from quantities first, a status string only as a fallback.

    Quantities are numeric and unambiguous; a status enum's spelling is not
    confirmed anywhere this project has a source for, so it is only used to
    catch what no quantity comparison can reveal - cancellation, rejection,
    and whether an amount order that names no share count is finished.
    """
    if filled_qty is not None and filled_qty > 0:
        if ordered_qty is not None:
            return (
                STATUS_FILLED if filled_qty >= ordered_qty else STATUS_PARTIALLY_FILLED
            )
        return _complete_without_a_target(payload)

    raw_status = str(_pick(payload, _STATUS_KEYS) or "").lower()
    if any(hint in raw_status for hint in _CANCEL_HINTS):
        return STATUS_CANCELED
    if any(hint in raw_status for hint in _REJECT_HINTS):
        return STATUS_REJECTED
    return None  # still open - nothing changed


class Reconciler:
    def __init__(self, trading, store, oco_expire_days=30, oco_stop_loss_slippage=Decimal("0.005"), clock=None):
        if trading.mode is not TradingMode.LIVE:
            raise ValueError(
                "Reconciler는 LIVE TradingApi가 필요합니다 - PAPER 주문은 브로커에 "
                "전송된 적이 없어 조회할 대상이 없습니다."
            )
        self.trading = trading
        self.store = store
        self.oco_expire_days = oco_expire_days
        self.oco_stop_loss_slippage = oco_stop_loss_slippage
        self.clock = clock or (lambda: datetime.now())

    def run(self):
        """Reconcile every open order. Returns a list of outcome strings."""
        results = []
        for order in self.store.pending_orders(mode=TradingMode.LIVE.value):
            results.append(self._reconcile_one(order))
        return results

    # ------------------------------------------------------------ internals

    def _reconcile_one(self, order):
        client_order_id = order["client_order_id"]
        payload = self._fetch(order)
        if payload is _UNREADABLE:
            return f"{client_order_id}: 조회 실패, 다음 실행에서 재시도"
        if payload is None:
            # The history was read and this order is not in it. Not marked
            # failed: an order id absent from a list whose query shape this
            # project has no confirmed source for is weak evidence, and
            # "failed" would assert that no shares were bought. Said out loud
            # instead, because an order stuck here needs a human, not another
            # poll in thirty minutes.
            return (
                f"{client_order_id}: 브로커 주문 이력에 없습니다 — "
                "접수되지 않았을 수 있습니다. 확인이 필요합니다."
            )

        ordered_qty = to_decimal(order.get("quantity"))
        filled_qty = _pick_decimal(payload, _FILLED_QTY_KEYS)
        already_filled = to_decimal(order.get("filled_quantity")) or Decimal(0)

        new_status = _infer_status(payload, filled_qty, ordered_qty)
        order_id = order.get("order_id") or _pick(payload, _ORDER_ID_KEYS)

        updates = {}
        if order_id and not order.get("order_id"):
            updates["order_id"] = order_id

        newly_filled = Decimal(0)
        if filled_qty is not None and filled_qty > already_filled:
            newly_filled = filled_qty - already_filled
            avg_price = _pick_decimal(payload, _AVG_PRICE_KEYS) or to_decimal(order.get("price"))
            self.store.save_fill(
                order_id or client_order_id,
                quantity=newly_filled,
                price=avg_price,
                commission=_pick_decimal(payload, _COMMISSION_KEYS),
                tax=_pick_decimal(payload, _TAX_KEYS),
            )
            updates["filled_quantity"] = filled_qty

        if new_status is not None and new_status != order.get("status"):
            updates["status"] = new_status

        if updates:
            self.store.update_order(client_order_id, **updates)

        oco_note = ""
        if newly_filled > 0 and order.get("side") == "BUY":
            oco_note = self._arm_bracket(order, filled_qty or newly_filled)

        return (
            f"{client_order_id}: {new_status or order.get('status')}"
            f"{f' (체결 {newly_filled})' if newly_filled else ''}{oco_note}"
        )

    def _fetch(self, order):
        """The broker's current view of one order.

        None means the broker answered and has no such order - a fact about
        the order. ``_UNREADABLE`` means the question could not be asked.
        """
        order_id = order.get("order_id")
        try:
            if order_id:
                return self.trading.get_order(order_id) or {}
            return self._find_by_client_id(order["client_order_id"])
        except TossApiError:
            return _UNREADABLE

    def _find_by_client_id(self, client_order_id):
        """Scan the order history for a match, for orders with no orderId yet.

        This is the path an ``unknown``-status order takes: the executor
        never received an orderId, so the only way back to this order is to
        look for its clientOrderId in the history list.
        """
        history = self.trading.list_orders() or []
        if isinstance(history, dict):
            history = history.get("orders") or history.get("items") or []
        for entry in history:
            if _pick(entry, _CLIENT_ID_KEYS) == client_order_id:
                return entry
        return None

    def _arm_bracket(self, order, filled_qty):
        """Register the OCO bracket for a newly-filled BUY, once."""
        if order.get("oco_client_order_id"):
            return ""  # already armed on a previous reconcile pass

        stop_loss = to_decimal(order.get("stop_loss_price"))
        take_profit = to_decimal(order.get("take_profit_price"))
        if stop_loss is None or take_profit is None:
            # This signal did not ask for a bracket. Every momentum-dca buy
            # currently lands here on purpose (see module docstring) - the
            # strategy carries no per-position stop until step 8's backtest
            # justifies a distance. Not a warning, just the current state.
            return ""

        entry_id = order["client_order_id"]
        oco_id = f"oco-{entry_id}"[:64]
        expire_date = (self.clock() + timedelta(days=self.oco_expire_days)).strftime(
            "%Y-%m-%d"
        )
        currency = order.get("currency") or "KRW"

        body = conditional_order_body(
            oco_id,
            order["symbol"],
            filled_qty,
            take_profit,
            stop_loss,
            expire_date,
            currency=currency,
            stop_loss_slippage=self.oco_stop_loss_slippage,
        )

        self.store.save_conditional_order(
            oco_id,
            entry_client_order_id=entry_id,
            symbol=order["symbol"],
            quantity=filled_qty,
            take_profit_price=take_profit,
            stop_loss_price=stop_loss,
            expire_date=expire_date,
            status="pending",
            mode=TradingMode.LIVE.value,
        )

        try:
            self.trading.place_conditional_order(body)
        except TossApiError as exc:
            # Not raised further: a bracket that failed to register is
            # important but is not the same failure class as our own id
            # colliding with a different order's (executor.ExecutorBug) - it
            # does not mean anything about this run's other orders.
            self.store.update_conditional_order(oco_id, status="failed", error_code=exc.code)
            self.store.update_order(entry_id, oco_status="failed")
            return f" · OCO 등록 실패({exc.code})"

        self.store.update_conditional_order(oco_id, status="registered")
        self.store.update_order(
            entry_id, oco_client_order_id=oco_id, oco_status="registered"
        )
        return f" · OCO 등록({oco_id})"
