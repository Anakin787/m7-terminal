"""Sending an approved OrderIntent, once.

Everything here exists to make one guarantee: a signal that has been acted on
is never acted on twice. The client_order_id is derived rather than random
(:mod:`src.execution.ids`), the row is written before the request goes out,
and an id already present in the store short-circuits the whole path.

The error policy is design section 3.4. Its shape is worth stating plainly:
almost nothing is retried. A rejected order is information about the account
or the market, and repeating the request only produces the same rejection at
a higher rate-limit cost. The two exceptions are a price the market moved out
from under (retried once, after re-reading the band) and an ambiguous
in-flight response (resolved by *asking*, never by re-sending).
"""

from dataclasses import dataclass, replace
from datetime import datetime

from src.execution.ids import make_client_order_id
from src.models import to_decimal
from src.toss.errors import TERMINAL_CODES, TossApiError
from src.toss.trading import TradingMode, order_body

STATUS_PENDING = "pending"
STATUS_SIMULATED = "simulated"
STATUS_SUBMITTED = "submitted"
STATUS_FAILED = "failed"
STATUS_UNKNOWN = "unknown"

#: A price that was legal when the strategy chose it and is not legal now.
#: Worth exactly one retry with a freshly clamped price - the band does not
#: move again within a run.
RETRYABLE_PRICE_CODES = frozenset({"price-out-of-range", "invalid-tick-size"})

#: The request may or may not have been accepted. Re-sending could double the
#: position, so the only safe move is to look it up.
IN_FLIGHT_CODES = frozenset({"request-in-progress", "already-processing"})

#: Rejections that disqualify a symbol or the whole account until a human
#: intervenes. Surfaced separately so the caller can stop touching them.
BLACKLIST_CODES = frozenset(
    {"stock-restricted", "account-restricted", "prerequisite-required"}
)


class ExecutorBug(Exception):
    """Raised when the store and the broker disagree about our own ids.

    ``idempotency-key-conflict`` means Toss has seen this client_order_id
    attached to a *different* order body. That cannot happen unless id
    generation is broken, and if it is broken then no order's idempotency can
    be trusted - so this stops the run rather than skipping one signal.
    """


@dataclass
class OrderRecord:
    """What happened to one intent."""

    client_order_id: str
    status: str
    order_id: str | None = None
    error_code: str | None = None
    detail: str = ""
    #: True when submit() found the order already recorded and did nothing.
    duplicate: bool = False
    #: The symbol or the account is restricted; stop sending orders for it.
    blacklisted: bool = False
    #: The failure is one src.toss.errors.TERMINAL_CODES names - a known,
    #: expected rejection rather than a surprise worth investigating.
    terminal: bool = False

    @property
    def sent(self):
        return self.status in (STATUS_SUBMITTED, STATUS_SIMULATED)


class OrderExecutor:
    def __init__(
        self, trading, store, mode=None, clock=None, price_limits=None,
        session_date=None,
    ):
        self.trading = trading
        self.store = store
        #: The trading day these orders belong to, which is what the
        #: client_order_id is keyed on. Falls back to the wall clock when the
        #: caller has no session to name (tests, and any path with no bars),
        #: which is what this did before - and is wrong for the scheduled
        #: 23:35 KST run, whose retry window crosses KST midnight.
        self.session_date = session_date
        #: ``{symbol: (low, high)}``, normally the same mapping the risk gate
        #: read. Used to clamp a price the market moved away from; without it
        #: a price rejection is simply not retried.
        self.price_limits = price_limits or {}
        # The trading API already carries a mode and owns the client whose
        # write permission follows it. Taking a second opinion here would let
        # the two disagree, which is exactly the confusion design section 7
        # lists as a risk.
        self.mode = TradingMode.parse(mode) if mode is not None else trading.mode
        if self.mode is not trading.mode:
            raise ValueError(
                f"executor 모드({self.mode.value})가 TradingApi 모드"
                f"({trading.mode.value})와 다릅니다."
            )
        self.clock = clock or (lambda: datetime.now())
        #: Per-run sequence per (strategy, symbol, day).
        #:
        #: Deliberately in memory rather than counted from the orders table.
        #: A row count would give a re-run of the same batch a *higher* seq,
        #: hence a new client_order_id, hence a second real order - the exact
        #: failure the derived id exists to prevent. Starting from zero every
        #: run means the same batch derives the same ids and the store
        #: recognises them. This leans on strategies being pure: the same
        #: context yields the same signals in the same order.
        self._seq = {}

    # ------------------------------------------------------------- public

    def submit(self, intent, signal_id=None):
        """Place one approved intent. Returns an :class:`OrderRecord`."""
        intent = self._identify(intent)
        client_order_id = intent.client_order_id

        existing = self.store.order_by_client_id(client_order_id)
        if existing is not None:
            return OrderRecord(
                client_order_id=client_order_id,
                status=existing["status"],
                order_id=existing["order_id"],
                error_code=existing["error_code"],
                detail="이미 기록된 주문입니다. 재발주하지 않았습니다.",
                duplicate=True,
            )

        # Written before the request. An unanswered POST still leaves this
        # row, so the next run sees the id and stops instead of re-ordering.
        self.store.save_order(
            intent,
            signal_id=signal_id,
            status=STATUS_PENDING,
            mode=self.mode.value,
            session_date=self.session_date,
        )

        if self.mode is TradingMode.PAPER:
            return self._finish(client_order_id, STATUS_SIMULATED)

        return self._send(intent, retry_price=True)

    # ------------------------------------------------------------ internals

    def _identify(self, intent):
        if intent.client_order_id:
            return intent

        # The session date, not today's date. Two attempts at the same run
        # must derive the same id or the second one is a second real order,
        # and the scheduled run starts 25 minutes before the wall-clock date
        # it would otherwise be keyed on rolls over.
        day = (
            str(self.session_date)[:10]
            if self.session_date is not None
            else self.clock().strftime("%Y-%m-%d")
        )
        key = (intent.strategy, intent.symbol, day)
        seq = self._seq[key] = self._seq.get(key, 0) + 1
        return intent.with_client_order_id(
            make_client_order_id(intent.strategy, intent.symbol, day, seq)
        )

    def _send(self, intent, retry_price):
        client_order_id = intent.client_order_id
        try:
            result = self.trading.place_order(order_body(intent)) or {}
        except TossApiError as exc:
            return self._handle_error(intent, exc, retry_price)

        return self._finish(
            client_order_id, STATUS_SUBMITTED, order_id=result.get("orderId")
        )

    def _handle_error(self, intent, exc, retry_price):
        code = exc.code
        client_order_id = intent.client_order_id

        if code == "idempotency-key-conflict":
            self._finish(client_order_id, STATUS_FAILED, error_code=code)
            raise ExecutorBug(
                f"client_order_id '{client_order_id}'가 다른 내용의 주문으로 이미 "
                f"사용됐습니다. ID 생성 로직이 깨졌을 가능성이 높아 실행을 중단합니다. "
                f"({exc})"
            ) from exc

        if code in IN_FLIGHT_CODES:
            # Never re-send. Ask what happened instead - and if we cannot find
            # out, say so rather than guessing, because both guesses are bad.
            return self._resolve_in_flight(client_order_id, exc)

        if code in RETRYABLE_PRICE_CODES and retry_price:
            reclamped = self._reclamp(intent, exc)
            if reclamped is not None:
                return self._send(reclamped, retry_price=False)

        # Everything else fails without a retry. src.toss.errors.TERMINAL_CODES
        # names the ones this is expected for; codes outside it land here too,
        # on purpose - an error we do not recognise is not one we understand
        # well enough to retry safely.
        return self._finish(
            client_order_id,
            STATUS_FAILED,
            error_code=code,
            detail=str(exc),
            terminal=code in TERMINAL_CODES,
            blacklisted=code in BLACKLIST_CODES,
        )

    def _resolve_in_flight(self, client_order_id, exc):
        """Record an ambiguous request as unresolved, and stop.

        The order may or may not exist. Re-sending could double the position,
        so that is off the table; and it cannot be looked up here either,
        because ``GET /orders/{orderId}`` needs the id Toss assigns, which is
        exactly what we did not receive. Nothing in the confirmed endpoint
        list (design 2.2) resolves a clientOrderId, so inventing a lookup
        would be guesswork on the one path where guessing is most expensive.

        Left as "unknown" on purpose: not "failed", because the order may
        exist; not retried, because the order may exist. The reconciler in
        step [9] settles these against the order history.
        """
        return self._finish(
            client_order_id,
            STATUS_UNKNOWN,
            error_code=exc.code,
            detail=(
                "처리 중 응답을 받아 재발주하지 않았습니다. "
                "체결 여부는 주문 이력으로 확인해야 합니다."
            ),
        )

    def _reclamp(self, intent, exc):
        """Pull a limit price back inside today's band, for one retry.

        The bounds come from the error envelope when Toss supplies them, and
        otherwise from the band the risk gate already read. With neither there
        is nothing to correct, so the retry is skipped rather than guessed at
        - a re-sent order at an invented price is worse than a failed one.
        """
        if intent.limit_price is None:
            return None

        low, high = _band_from_error(exc)
        if low is None and high is None:
            low, high = self.price_limits.get(intent.symbol, (None, None))
        if low is None and high is None:
            return None

        price = intent.limit_price
        if high is not None and price > high:
            price = high
        elif low is not None and price < low:
            price = low
        else:
            # Already inside the band we know about, so the rejection was
            # about something else (a tick size, say) that clamping cannot fix.
            return None

        return replace(intent, limit_price=price)

    def _finish(
        self, client_order_id, status, order_id=None, error_code=None, detail="",
        blacklisted=False, terminal=False,
    ):
        fields = {"status": status}
        if order_id:
            fields["order_id"] = order_id
        if error_code:
            fields["error_code"] = error_code
        self.store.update_order(client_order_id, **fields)
        return OrderRecord(
            client_order_id=client_order_id,
            status=status,
            order_id=order_id,
            error_code=error_code,
            detail=detail,
            blacklisted=blacklisted,
            terminal=terminal,
        )


#: Spellings Toss has used for the band in a price-out-of-range envelope.
_LOW_KEYS = ("lowerLimit", "lowerLimitPrice", "minPrice", "lower")
_HIGH_KEYS = ("upperLimit", "upperLimitPrice", "maxPrice", "upper")


def _band_from_error(exc):
    """Read (low, high) out of an error envelope's ``data``, if present."""
    data = getattr(exc, "data", None)
    if not isinstance(data, dict):
        return None, None

    def pick(keys):
        for key in keys:
            value = to_decimal(data.get(key))
            if value is not None:
                return value
        return None

    return pick(_LOW_KEYS), pick(_HIGH_KEYS)
