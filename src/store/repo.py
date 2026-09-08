"""Firestore persistence for snapshots and report history.

The Portfolio Value chart cannot be reconstructed after the fact - Toss
reports today's numbers, not last month's - so every report run appends one
snapshot document. Starting this early is the whole point.

Monetary values are stored as strings, not Firestore's native double: a
double is a float and would reintroduce exactly the rounding drift the
Decimal pipeline removes. Read them back through Decimal.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from google.api_core.exceptions import AlreadyExists
from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from src.models import to_decimal
from src.strategy.base import DailyUsage

#: Ranges offered by the dashboard's chart selector.
RANGE_DAYS = {
    "1W": 7,
    "1M": 30,
    "3M": 90,
    "1Y": 365,
    "ALL": None,
}


def _text(value):
    return None if value is None else str(value)


def _json_safe(value):
    """Recursively coerce a value to Firestore-storable types.

    ``signal.meta`` is free-form strategy output and often carries
    ``Decimal`` (e.g. an RSI reading) - a type Firestore's client rejects
    outright, unlike SQLite where it went through ``json.dumps(default=str)``.
    """
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


#: Every timestamp in this store is Seoul wall-clock, written naive so the
#: string format stays byte-identical to the rows already in Firestore (these
#: strings are document IDs and are range-queried as text). A fixed +09:00 is
#: exact - Korea has no DST - and needs no tzdata, which a slim container has
#: no reason to carry. ``datetime.now()`` would follow the host: fine on the
#: PC this started on, nine hours off inside a UTC container, which would
#: interleave new rows with the old ones in the wrong order.
_KST = timezone(timedelta(hours=9))


def _clock():
    return datetime.now(_KST).replace(tzinfo=None)


def _now(precise=False):
    now = _clock()
    return now.isoformat() if precise else now.replace(microsecond=0).isoformat()


class EmptySnapshot(ValueError):
    """A snapshot with nothing in it, refused before it reaches the chart."""


class Store:
    def __init__(self, client=None):
        self.client = client or firestore.Client()

    # ------------------------------------------------------------- snapshots

    def save_snapshot(self, snapshot, ts=None):
        """Write one snapshot, keyed by ``ts`` so re-runs overwrite in place.

        An empty snapshot is refused rather than stored. The chart cannot be
        backfilled, so a bad row is permanent until someone deletes it by
        hand, and "no holdings" is far more often a misconfigured run than a
        liquidated account - see ``require_config_file``.
        """
        if not snapshot.positions and to_decimal(snapshot.total_krw, default=0) <= 0:
            raise EmptySnapshot(
                "보유 종목이 없고 총자산도 0원인 스냅샷은 저장하지 않습니다. "
                "설정이 실제로 읽혔는지 확인하세요."
            )

        ts = ts or _now()
        doc = self.client.collection("snapshots").document(ts)
        doc.set(
            {
                "ts": ts,
                "total_krw": _text(snapshot.total_krw),
                "purchase_krw": _text(snapshot.purchase_krw),
                "profit_krw": _text(snapshot.profit_krw),
                "profit_rate": _text(snapshot.profit_rate),
                "profit_after_cost_krw": _text(snapshot.profit_after_cost_krw),
                "profit_rate_after_cost": _text(snapshot.profit_rate_after_cost),
                "daily_profit_krw": _text(snapshot.daily_profit_krw),
                "daily_profit_rate": _text(snapshot.daily_profit_rate),
                "exchange_rate": _text(snapshot.exchange_rate),
                "has_unconverted_fx": bool(snapshot.has_unconverted_fx),
            }
        )

        positions = doc.collection("positions")
        for existing in positions.stream():
            existing.reference.delete()
        for position in snapshot.positions:
            positions.document().set(
                {
                    "symbol": position.symbol,
                    "name": position.name,
                    "market_country": position.market_country,
                    "currency": position.currency,
                    "quantity": _text(position.quantity),
                    "last_price": _text(position.last_price),
                    "avg_price": _text(position.avg_purchase_price),
                    "market_value": _text(position.evaluation),
                    "profit_loss": _text(position.profit_loss),
                    "profit_rate": _text(position.profit_rate),
                    "daily_profit_loss": _text(position.daily_profit_loss),
                    "source": position.source,
                }
            )
        return ts

    def history(self, range_key="3M"):
        """Return snapshot rows for the chart, oldest first."""
        days = RANGE_DAYS.get(str(range_key).upper(), 90)
        query = self.client.collection("snapshots")
        if days is not None:
            since = (_clock() - timedelta(days=days)).replace(microsecond=0)
            query = query.where(filter=FieldFilter("ts", ">=", since.isoformat()))
        query = query.order_by("ts")

        return [
            {
                "ts": doc.get("ts"),
                "total_krw": to_decimal(doc.get("total_krw"), default=0),
                "profit_krw": to_decimal(doc.get("profit_krw"), default=0),
                "profit_rate": to_decimal(doc.get("profit_rate"), default=0),
            }
            for doc in query.stream()
        ]

    def latest_snapshot(self):
        docs = list(
            self.client.collection("snapshots")
            .order_by("ts", direction=firestore.Query.DESCENDING)
            .limit(1)
            .stream()
        )
        if not docs:
            return None
        return docs[0].to_dict()

    def snapshot_count(self):
        return self.client.collection("snapshots").count().get()[0][0].value

    # ------------------------------------------------------ name overrides

    def symbol_names(self):
        """Return {symbol: display name} for every override."""
        return {
            doc.id: doc.get("name")
            for doc in self.client.collection("symbol_overrides").stream()
        }

    def set_symbol_name(self, symbol, name):
        """Store a display name, or clear it when name is blank."""
        name = (name or "").strip()
        doc = self.client.collection("symbol_overrides").document(symbol)
        if not name:
            doc.delete()
            return None
        doc.set({"name": name, "updated_at": _now()})
        return name

    # --------------------------------------------------------------- reports

    def save_report(self, page_id, title=None, url=None, ai_comment=None, ts=None):
        ts = ts or _now()
        self.client.collection("reports").document(page_id).set(
            {"ts": ts, "title": title, "url": url, "ai_comment": ai_comment}
        )
        return ts

    def reports_page(self, limit=20, offset=0):
        """One page of reports, newest first, with the total behind it.

        ``offset`` is pushed to Firestore, which bills the skipped documents
        as reads - so page 10 costs ten pages' worth. Accepted here because
        reports arrive once a day: a year is a dozen pages, and the honest
        page numbers that offsets buy are worth more than the reads a cursor
        would save. A collection that grew by the minute would need the
        cursor instead.

        The count is an aggregation query, not a scan; it does not read the
        documents it counts.
        """
        collection = self.client.collection("reports")
        query = (
            collection.order_by("ts", direction=firestore.Query.DESCENDING)
            .offset(offset)
            .limit(limit)
        )
        rows = [{"page_id": doc.id, **doc.to_dict()} for doc in query.stream()]
        return {"reports": rows, "total": self._count(collection), "offset": offset,
                "limit": limit}

    @staticmethod
    def _count(collection):
        """``len()`` for a Firestore collection, without reading it.

        Falls back to None rather than raising: a pager with no total still
        pages, and a count that fails must not take the table down with it.
        """
        try:
            result = collection.count().get()
            return int(result[0][0].value)
        except Exception:  # noqa: BLE001 - see docstring
            return None

    # ------------------------------------------------------------ audit log

    def save_audit_entries(self, entries):
        """Append audit entries. Append-only by design - nothing here updates.

        An audit log whose rows can be edited in place answers a different,
        much weaker question than the one it was built for.
        """
        collection = self.client.collection("audit_log")
        written = 0
        for entry in entries or []:
            collection.add(_json_safe(dict(entry)))
            written += 1
        return written

    #: How many ordered rows a filtered audit read scans before giving up.
    _AUDIT_SCAN = 500

    def audit_page(self, limit=50, offset=0, category=None):
        """One page of audit entries, newest first, optionally one category.

        Only the ordering is pushed to Firestore; the category is matched
        here. Combining ``where`` with an ``order_by`` on a different field
        needs a composite index created per deployment - and the emulator
        accepts such a query happily, so the failure would first appear in
        production. Every other range scan in this module filters in Python
        for the same reason.

        Which is also why the paging is done here rather than with Firestore
        ``offset``: an offset applied before the category filter would skip
        rows of *other* categories and hand back the wrong page. So one
        ordered scan, filtered, then sliced - and ``total`` comes free from
        the same pass.

        ``truncated`` says the scan hit its ceiling, so the total is a floor
        rather than a count. Reported rather than hidden: a pager that
        silently stops at page 20 of an unknown number is the kind of quiet
        this project keeps paying for.
        """
        query = self.client.collection("audit_log").order_by(
            "detected_at", direction=firestore.Query.DESCENDING
        )
        scan = self._AUDIT_SCAN
        matched, scanned = [], 0
        for doc in query.limit(scan).stream():
            scanned += 1
            data = doc.to_dict() or {}
            if category and data.get("category") != category:
                continue
            matched.append({"id": doc.id, **data})
        return {
            "entries": matched[offset : offset + limit],
            "total": len(matched),
            "truncated": scanned >= scan,
            "offset": offset,
            "limit": limit,
        }

    def audit_fingerprint(self):
        """The audited settings as of the last run, or None on a first run.

        None and ``{}`` mean different things here - "never recorded" versus
        "recorded, and everything was empty" - so the absent document returns
        None and the diff treats it as a silent baseline rather than as 39
        symbols being added at once.
        """
        doc = self.client.collection("audit_state").document("fingerprint").get()
        if not doc.exists:
            return None
        return (doc.to_dict() or {}).get("settings")

    def save_audit_fingerprint(self, settings, ts=None):
        self.client.collection("audit_state").document("fingerprint").set(
            {"settings": _json_safe(settings), "ts": ts or _now(precise=True)}
        )

    # ------------------------------------------------- universe review (AI)

    def save_universe_review(self, review, ttl_days=7, ts=None):
        """Persist one AI universe review: vetoes that expire, advice that does not.

        Vetoes are keyed by symbol and carry ``expires_at``, so a veto nobody
        renews lapses on its own. Nothing an AI said yesterday stays in force
        because the batch job stopped running - and re-raising the same veto
        tomorrow simply pushes the expiry out.

        Candidates are appended, never keyed: the point of the list is what it
        suggested and when, so a later run must not overwrite the record of an
        earlier suggestion the user is still thinking about.
        """
        ts = ts or _now(precise=True)
        expires_at = (
            datetime.fromisoformat(ts) + timedelta(days=max(int(ttl_days), 0))
        ).isoformat()

        vetoes = self.client.collection("universe_vetoes")
        for veto in getattr(review, "vetoes", ()) or ():
            vetoes.document(veto.symbol).set(
                {
                    "symbol": veto.symbol,
                    "category": veto.category,
                    "reason": veto.reason,
                    "evidence": veto.evidence,
                    "ts": ts,
                    "expires_at": expires_at,
                }
            )

        candidates = [
            {
                "symbol": candidate.symbol,
                "name": candidate.name,
                "reason": candidate.reason,
            }
            for candidate in getattr(review, "candidates", ()) or ()
        ]
        if candidates:
            self.client.collection("universe_candidates").document(ts).set(
                {"ts": ts, "candidates": candidates}
            )
        return ts

    def active_vetoes(self, now=None):
        """``{symbol: reason}`` for vetoes that have not expired.

        Expiry is compared here rather than in the query for the same reason
        every other range scan in this module is: a composite index per
        deployment is real setup friction for a collection this small.
        """
        now = (now or _clock()).isoformat()
        active = {}
        for doc in self.client.collection("universe_vetoes").stream():
            data = doc.to_dict() or {}
            expires_at = data.get("expires_at")
            if expires_at and expires_at <= now:
                continue
            active[doc.id] = data.get("reason") or data.get("category") or "AI 보류"
        return active

    def clear_veto(self, symbol):
        """Lift one veto immediately, without waiting for it to expire."""
        self.client.collection("universe_vetoes").document(symbol).delete()

    def latest_universe_candidates(self, limit=1):
        """The most recent candidate suggestions, newest run first."""
        query = (
            self.client.collection("universe_candidates")
            .order_by("ts", direction=firestore.Query.DESCENDING)
            .limit(limit)
        )
        return [doc.to_dict() for doc in query.stream()]

    # --------------------------------------------------------------- signals

    def save_decision(self, decision, ts=None):
        """Record one risk-gate decision and return its Firestore doc id.

        Accepted and rejected signals go into the same collection so the
        audit trail reads in one pass; a rejection additionally carries the
        rule that stopped it, inlined on the same document. Called for every
        signal, not only the ones that trade.
        """
        signal = decision.signal
        ts = ts or _now(precise=True)

        data = {
            "ts": ts,
            "strategy": signal.strategy,
            "symbol": signal.symbol,
            "side": signal.side,
            "order_type": signal.order_type,
            "quantity": _text(signal.quantity),
            "amount": _text(signal.amount),
            "limit_price": _text(signal.limit_price),
            "currency": signal.currency,
            "reason": signal.reason,
            "stop_loss_price": _text(signal.stop_loss_price),
            "take_profit_price": _text(signal.take_profit_price),
            "payload": _json_safe(signal.meta) if signal.meta else None,
            "outcome": "accepted" if decision.approved else "rejected",
        }
        if decision.rejection is not None:
            data["rejection"] = {
                "rule": decision.rejection.rule,
                "detail": decision.rejection.detail,
            }

        _, doc_ref = self.client.collection("signals").add(data)
        return doc_ref.id

    def recent_signals(self, limit=50):
        """Signals newest first, each with the rule that rejected it, if any."""
        query = (
            self.client.collection("signals")
            .order_by("ts", direction=firestore.Query.DESCENDING)
            .limit(limit)
        )
        rows = []
        for doc in query.stream():
            data = doc.to_dict()
            rejection = data.pop("rejection", None) or {}
            rows.append(
                {
                    "id": doc.id,
                    **data,
                    "reject_rule": rejection.get("rule"),
                    "reject_detail": rejection.get("detail"),
                }
            )
        return rows

    # ---------------------------------------------------------------- orders

    def daily_usage(self, day=None):
        """Today's order count and total notional, for the risk gate.

        Counts what was actually sent, so a rejected signal never consumes
        budget. Paper orders are counted too: a paper run that would have
        blown the daily limit should say so rather than look clean.

        Summed in Python rather than via a Firestore aggregation query - the
        order volume here is small enough that this is simpler, and it keeps
        the total on Decimal instead of a server-side float.
        """
        day = day or _clock().strftime("%Y-%m-%d")
        next_day = (datetime.strptime(day, "%Y-%m-%d") + timedelta(days=1)).strftime(
            "%Y-%m-%d"
        )
        query = (
            self.client.collection("orders")
            .where(filter=FieldFilter("ts", ">=", day))
            .where(filter=FieldFilter("ts", "<", next_day))
        )

        count = 0
        total = Decimal(0)
        for doc in query.stream():
            data = doc.to_dict()
            if data.get("status") == "rejected":
                continue
            count += 1
            total += to_decimal(data.get("notional_krw"), default=0) or Decimal(0)

        return DailyUsage(order_count=count, notional_krw=total.quantize(Decimal("1")))

    def save_order(self, intent, signal_id=None, status="pending", mode="paper", ts=None):
        """Record an order before it is sent.

        Written ahead of the request on purpose: if the response never
        arrives, the attempt still left a trace, and the next run finds the
        client_order_id already taken rather than placing the order again.

        ``stop_loss_price``/``take_profit_price`` are copied from the signal
        that produced this order, not looked up later, because the
        reconciler needs them once the entry fills and has no other way back
        to the strategy's original intent - only the persisted order.
        """
        ts = ts or _now(precise=True)
        signal = intent.signal
        doc = self.client.collection("orders").document(intent.client_order_id)
        try:
            doc.create(
                {
                    "signal_id": signal_id,
                    "ts": ts,
                    "strategy": intent.strategy,
                    "symbol": intent.symbol,
                    "side": intent.side,
                    "order_type": intent.order_type,
                    "quantity": _text(intent.quantity),
                    "amount": _text(intent.amount),
                    "price": _text(intent.limit_price),
                    "currency": intent.currency,
                    "notional_krw": _text(intent.notional_krw),
                    "status": status,
                    "mode": mode,
                    "order_id": None,
                    "error_code": None,
                    "stop_loss_price": _text(getattr(signal, "stop_loss_price", None)),
                    "take_profit_price": _text(getattr(signal, "take_profit_price", None)),
                    "filled_quantity": "0",
                    "oco_client_order_id": None,
                    "oco_status": None,
                    "updated_at": ts,
                }
            )
        except AlreadyExists:
            pass
        return ts

    def update_order(self, client_order_id, **fields):
        """Patch an order document. Unknown columns are refused, not ignored."""
        allowed = {
            "order_id", "status", "error_code", "signal_id",
            "filled_quantity", "oco_client_order_id", "oco_status",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"수정할 수 없는 컬럼입니다: {sorted(unknown)}")
        if not fields:
            return None

        if "filled_quantity" in fields:
            fields["filled_quantity"] = _text(fields["filled_quantity"])

        fields["updated_at"] = _now(precise=True)
        self.client.collection("orders").document(client_order_id).update(fields)
        return fields["updated_at"]

    def order_by_client_id(self, client_order_id):
        doc = self.client.collection("orders").document(client_order_id).get()
        if not doc.exists:
            return None
        return {"client_order_id": doc.id, **doc.to_dict()}

    def recent_orders(self, limit=50):
        query = (
            self.client.collection("orders")
            .order_by("ts", direction=firestore.Query.DESCENDING)
            .limit(limit)
        )
        return [{"client_order_id": doc.id, **doc.to_dict()} for doc in query.stream()]

    #: Order states the reconciler still has work to do for. Everything else
    #: (``simulated``, ``failed``, ``filled``, ``canceled``, ``rejected``) is
    #: a resting state with nothing left to poll for.
    _OPEN_ORDER_STATUSES = ["submitted", "unknown", "partially_filled"]

    def pending_orders(self, mode="live"):
        """Orders not yet settled. Filtered by mode - there is nothing to
        poll a broker about for a PAPER order that was never sent."""
        query = self.client.collection("orders").where(
            filter=FieldFilter("mode", "==", mode)
        ).where(filter=FieldFilter("status", "in", self._OPEN_ORDER_STATUSES))
        return [{"client_order_id": doc.id, **doc.to_dict()} for doc in query.stream()]

    # ---------------------------------------------------------------- fills

    def save_fill(self, order_id, quantity, price, commission=None, tax=None, ts=None):
        """Append one fill. Firestore assigns the id, since several fills can
        arrive for the same order and none of their own fields are unique."""
        ts = ts or _now(precise=True)
        self.client.collection("fills").add(
            {
                "order_id": order_id,
                "ts": ts,
                "quantity": _text(quantity),
                "price": _text(price),
                "commission": _text(commission),
                "tax": _text(tax),
            }
        )
        return ts

    def fills_for_order(self, order_id):
        query = self.client.collection("fills").where(
            filter=FieldFilter("order_id", "==", order_id)
        )
        return [doc.to_dict() for doc in query.stream()]

    def recent_fills(self, limit=50):
        query = (
            self.client.collection("fills")
            .order_by("ts", direction=firestore.Query.DESCENDING)
            .limit(limit)
        )
        return [doc.to_dict() for doc in query.stream()]

    # --------------------------------------------------- conditional orders

    def save_conditional_order(
        self, client_order_id, entry_client_order_id, symbol, quantity,
        take_profit_price, stop_loss_price, expire_date, status, mode, ts=None,
    ):
        """Record an OCO bracket, keyed by its own client_order_id so a
        re-run of the reconciler recognises one it already placed."""
        ts = ts or _now(precise=True)
        doc = self.client.collection("conditional_orders").document(client_order_id)
        try:
            doc.create(
                {
                    "entry_client_order_id": entry_client_order_id,
                    "symbol": symbol,
                    "quantity": _text(quantity),
                    "take_profit_price": _text(take_profit_price),
                    "stop_loss_price": _text(stop_loss_price),
                    "expire_date": expire_date,
                    "status": status,
                    "mode": mode,
                    "error_code": None,
                    "ts": ts,
                    "updated_at": ts,
                }
            )
        except AlreadyExists:
            pass
        return ts

    def update_conditional_order(self, client_order_id, **fields):
        allowed = {"status", "error_code"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"수정할 수 없는 컬럼입니다: {sorted(unknown)}")
        if not fields:
            return None
        fields["updated_at"] = _now(precise=True)
        self.client.collection("conditional_orders").document(client_order_id).update(fields)
        return fields["updated_at"]

    def conditional_order_by_client_id(self, client_order_id):
        doc = self.client.collection("conditional_orders").document(client_order_id).get()
        if not doc.exists:
            return None
        return {"client_order_id": doc.id, **doc.to_dict()}

    def open_conditional_orders(self, limit=50):
        query = (
            self.client.collection("conditional_orders")
            .where(filter=FieldFilter("status", "==", "registered"))
            .limit(limit)
        )
        return [{"client_order_id": doc.id, **doc.to_dict()} for doc in query.stream()]
