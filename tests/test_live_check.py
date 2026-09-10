"""The step [10] tool: it must be hard to fire and impossible to fire quietly.

This is the one entry point that can place a real order - ``trade.py --live``
is still refused - so the tests that matter are the ones about refusing.
"""

import io
from decimal import Decimal

import pytest

from scripts import live_check
from src.strategy.base import SIDE_BUY, SIDE_SELL, Signal


def signal(**overrides):
    base = dict(
        strategy="live-check",
        symbol="SHY",
        side=SIDE_BUY,
        reason="설계 [10] 실거래 검증",
        order_type="MARKET",
        quantity=Decimal("1"),
        currency="USD",
    )
    base.update(overrides)
    return Signal(**base)


# ----------------------------------------------------------------- arguments


def test_it_defaults_to_one_share():
    """The size the design names, so the default cannot be an accident."""
    args = live_check.parse_args(["--symbol", "SHY"])
    assert args.quantity is None and args.amount is None
    # run() turns that into one share; the parser keeps None so that
    # --amount can tell "unset" from "explicitly 1".
    assert args.execute is False


def test_symbol_is_required():
    assert live_check.run(["--execute"]) == live_check.EXIT_REFUSED


def test_quantity_and_amount_are_mutually_exclusive():
    assert (
        live_check.run(["--symbol", "SHY", "--quantity", "1", "--amount", "5"])
        == live_check.EXIT_REFUSED
    )


# -------------------------------------------------------------- confirmation


def test_typing_the_symbol_confirms(monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO("SHY\n"))
    monkeypatch.setattr("sys.stdin.isatty", lambda: True, raising=False)
    monkeypatch.setattr("builtins.input", lambda: "SHY")

    assert live_check._confirm(signal(), assume_yes=False) is True


@pytest.mark.parametrize("typed", ["", "y", "yes", "shy", "SHYY", "SPY"])
def test_anything_but_the_symbol_cancels(monkeypatch, typed):
    """A bare "y" must not be enough - that is what makes it a decision."""
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    monkeypatch.setattr("sys.stdin.isatty", lambda: True, raising=False)
    monkeypatch.setattr("builtins.input", lambda: typed)

    assert live_check._confirm(signal(), assume_yes=False) is False


def test_a_non_interactive_run_refuses_rather_than_assuming(monkeypatch):
    """A scheduler or a pipe cannot answer, so it does not get to trade."""
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)

    assert live_check._confirm(signal(), assume_yes=False) is False


def test_yes_skips_the_prompt_without_reading_stdin(monkeypatch):
    def explode():
        raise AssertionError("--yes 인데도 입력을 읽으려 했습니다")

    monkeypatch.setattr("builtins.input", explode)

    assert live_check._confirm(signal(), assume_yes=True) is True


def test_an_interrupt_at_the_prompt_cancels(monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    monkeypatch.setattr("sys.stdin.isatty", lambda: True, raising=False)

    def interrupt():
        raise KeyboardInterrupt

    monkeypatch.setattr("builtins.input", interrupt)

    assert live_check._confirm(signal(), assume_yes=False) is False


# ------------------------------------------------------------------ currency


class FakeCtx:
    def __init__(self, position=None):
        self._position = position

    def position(self, symbol):
        return self._position


class FakePosition:
    def __init__(self, currency):
        self.currency = currency


def test_currency_defaults_to_usd():
    assert live_check._currency_of(FakeCtx(), "SHY") == "USD"


def test_a_held_position_names_its_own_currency():
    ctx = FakeCtx(FakePosition("KRW"))
    assert live_check._currency_of(ctx, "005930") == "KRW"


def test_a_position_with_no_currency_falls_back():
    ctx = FakeCtx(FakePosition(""))
    assert live_check._currency_of(ctx, "SHY") == "USD"


# --------------------------------------------------------------- the preview


def test_the_preview_shows_the_id_the_executor_will_actually_use():
    """A hand-built id would be a different string, and a lie about the order.

    make_client_order_id squashes anything outside its safe set, so the
    strategy name "live-check" reaches the broker as "live_check".
    """
    from src.execution.ids import make_client_order_id

    shown = make_client_order_id(live_check.STRATEGY_NAME, "SHY", "2026-09-09", 1)

    assert shown == "live_check-SHY-2026-09-09-1"
    assert shown != f"{live_check.STRATEGY_NAME}-SHY-2026-09-09-1"
