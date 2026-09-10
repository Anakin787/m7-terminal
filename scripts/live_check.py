"""Design step [10]: one real order, minimum size, through the real path.

The design opens LIVE on a single verified trade - "최소 수량 1주로 실거래
검증" - and until now there was nothing to do it with. ``trade.py`` only
knows how to act on what a strategy produced, and bucket-dca produces eight
orders worth several hundred dollars at once. That is not a first trade.

    python scripts/live_check.py --symbol SHY              # sends nothing
    python scripts/live_check.py --symbol SHY --execute    # buys 1 share, for real
    python scripts/live_check.py --symbol SHY --amount 5 --execute
    python scripts/live_check.py --settle                  # poll open LIVE orders

*Why it goes through the gate and the executor.* The point is to verify the
path, so nothing here reaches around it: the same ``build_context``, the same
``RiskGate``, the same ``OrderExecutor`` with the same derived
``client_order_id`` and the same ``session_date``. A script that placed an
order by calling the API directly would prove the API works and leave every
part this project actually wrote untested.

*Why an amount order is worth a second run.* bucket-dca buys are amount
orders exclusively, and the reconciler settles those on a different branch
than share-count orders - the one that could never reach ``filled`` until
2026-09-10. One share proves the path; a small amount order proves the
branch every real order will take.

*This is the one place LIVE opens.* ``trade.py --live`` is still refused, and
the scheduled Cloud Run job passes no such flag, so the engine cannot trade
for real by itself. This script can, which is why it does nothing without
``--execute`` and a typed confirmation, and why it is in no job's arguments.
Run it by hand, watch what happens, and settle it with ``--settle``.
"""

import argparse
import os
import sys
from datetime import date, timedelta
from decimal import Decimal

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config import ConfigFileMissing, load_config, require_config_file  # noqa: E402
from src.data.cache import BarCache  # noqa: E402
from src.data.loader import HistoryLoader  # noqa: E402
from src.data.yahoo import YahooBarSource  # noqa: E402
from src.execution.context import build_context  # noqa: E402
from src.execution.executor import OrderExecutor  # noqa: E402
from src.execution.ids import make_client_order_id  # noqa: E402
from src.execution.reconciler import Reconciler  # noqa: E402
from src.execution.risk import RiskGate  # noqa: E402
from src.pipeline import PortfolioService  # noqa: E402
from src.store.repo import Store  # noqa: E402
from src.strategy.base import ORDER_MARKET, SIDE_BUY, SIDE_SELL, Signal  # noqa: E402
from src.toss.errors import TossError  # noqa: E402
from src.toss.trading import TradingMode, build_trading_api, order_body  # noqa: E402

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_REJECTED = 2
EXIT_ERROR = 3

#: Enough history for the session date and nothing more. This script ranks
#: nothing, so it needs bars only to know which session it is trading.
LOOKBACK_DAYS = 30

STRATEGY_NAME = "live-check"


def _banner(execute):
    print("=" * 60)
    if execute:
        print("  M7 Terminal · 실거래 검증 · 모드: LIVE — 실제 주문이 나갑니다")
    else:
        print("  M7 Terminal · 실거래 검증 · 모드: 미리보기 (주문 없음)")
    print("=" * 60)


def _history(symbol):
    """Daily bars for one symbol, for the session date.

    Unlike the trading engine, a failure here is fatal rather than degraded.
    The engine can afford to produce no signals; this script's whole job is
    to place one order under a known session, and guessing that session from
    the wall clock is the bug that commit 1bee542 removed.
    """
    cache = BarCache()
    loader = HistoryLoader(cache, source=YahooBarSource(), offline=False)
    end = date.today()
    return loader.load([symbol], end - timedelta(days=LOOKBACK_DAYS), end)


def _confirm(signal, assume_yes):
    """Make the human type the symbol before real money moves."""
    if assume_yes:
        print(">>> --yes 가 주어져 확인을 건너뜁니다.")
        return True
    if not sys.stdin.isatty():
        print(
            "!!! 대화형 터미널이 아닙니다. 확인을 받을 수 없으므로 중단합니다.\n"
            "    자동 실행이 필요하다면 --yes 를 명시하세요.",
            file=sys.stderr,
        )
        return False

    what = (
        f"{signal.amount} {signal.currency}어치"
        if signal.amount is not None
        else f"{signal.quantity}주"
    )
    print()
    print(f"    실제 주문: {signal.symbol} {signal.side} {what} ({signal.order_type})")
    print(f"    계속하려면 종목코드를 그대로 입력하세요 (취소: 그 외 아무 입력): ", end="")
    try:
        typed = input().strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    if typed != signal.symbol:
        print(">>> 입력이 일치하지 않아 취소했습니다.")
        return False
    return True


def _describe(decision):
    if decision.approved:
        intent = decision.intent
        print(">>> 리스크 게이트: 승인")
        print(f"    주문 금액(원): {intent.notional_krw}")
        print(f"    주문 금액({intent.currency}): {intent.notional}")
        return
    print(f">>> 리스크 게이트: 거부 [{decision.rejection.rule}]")
    print(f"    {decision.rejection.detail}")


def _settle(config):
    """One reconciler pass, printing what it found."""
    store = Store()
    trading = build_trading_api(config, mode=TradingMode.LIVE)
    reconciler = Reconciler(
        trading,
        store,
        oco_expire_days=config.trading.oco_expire_days,
        oco_stop_loss_slippage=config.trading.oco_stop_loss_slippage,
    )
    print(">>> 미체결 LIVE 주문 정산 중...")
    results = reconciler.run()
    if not results:
        print("    정산할 LIVE 주문이 없습니다.")
        return EXIT_OK
    for line in results:
        print(f"    {line}")
    return EXIT_OK


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="설계 [10] 실거래 검증 — 최소 수량 1주짜리 LIVE 주문 1건"
    )
    parser.add_argument("--symbol", help="주문할 종목. 예: SHY")
    parser.add_argument(
        "--quantity", type=Decimal, default=None, help="주문 수량 (기본 1주)"
    )
    parser.add_argument(
        "--amount",
        type=Decimal,
        default=None,
        help="수량 대신 금액으로 주문합니다. bucket-dca가 쓰는 경로입니다.",
    )
    parser.add_argument("--sell", action="store_true", help="매수 대신 매도합니다.")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="실제로 주문을 전송합니다. 없으면 게이트까지만 돌고 아무것도 보내지 않습니다.",
    )
    parser.add_argument(
        "--yes", action="store_true", help="확인 입력을 건너뜁니다. 자동 실행 전용."
    )
    parser.add_argument(
        "--settle",
        action="store_true",
        help="주문을 내지 않고, 미체결 LIVE 주문의 체결만 확인합니다.",
    )
    return parser.parse_args(argv)


def run(argv=None):
    args = parse_args(argv)

    # Arguments are checked before the config is even read: a missing
    # --symbol is wrong whatever the config says, and reporting "config file
    # missing" for it would send the reader to the wrong problem.
    if not args.settle:
        if not args.symbol:
            print("ERROR: --symbol 이 필요합니다.", file=sys.stderr)
            return EXIT_REFUSED
        if args.quantity is not None and args.amount is not None:
            print(
                "ERROR: --quantity 와 --amount 는 함께 쓸 수 없습니다.", file=sys.stderr
            )
            return EXIT_REFUSED

    require_config_file()
    config = load_config()

    if args.settle:
        return _settle(config)

    symbol = args.symbol.upper()
    _banner(args.execute)

    store = Store()
    service = PortfolioService(config)

    print(f">>> {symbol} 과거 시세 로딩 중...")
    history = _history(symbol)

    print(">>> 컨텍스트 수집 중 (시세·잔고·장 운영시간)...")
    ctx = build_context(
        service,
        store,
        symbols=[symbol],
        kill_switch_path=config.trading.kill_switch_path,
        history=history,
    )
    print(
        f"    세션 {ctx.session_date} · 보유 {len(ctx.positions)}종목 · "
        f"이 세션 주문 {ctx.daily_usage.order_count}건"
    )
    if ctx.kill_switch:
        print("!!! KILL_SWITCH가 활성화되어 있습니다. 게이트가 거부합니다.")

    # Amount if asked for, otherwise a share count - defaulting to the one
    # share the design names.
    quantity = None if args.amount is not None else (args.quantity or Decimal("1"))
    signal = Signal(
        strategy=STRATEGY_NAME,
        symbol=symbol,
        side=SIDE_SELL if args.sell else SIDE_BUY,
        reason="설계 [10] 실거래 검증",
        order_type=ORDER_MARKET,
        quantity=quantity,
        amount=args.amount,
        currency=_currency_of(ctx, symbol),
    )

    decision = RiskGate(config.trading.risk_limits()).evaluate(signal, ctx)
    _describe(decision)
    if not decision.approved:
        return EXIT_REJECTED

    if not args.execute:
        # The body is shown rather than sent: this is the exact bytes the
        # LIVE run would POST, which is the last thing worth reading before
        # deciding to send it.
        # The real id generator, not an f-string that looks like one: it
        # squashes characters outside its safe set, so "live-check" becomes
        # "live_check" and a hand-built preview would show an id the executor
        # never uses.
        preview = decision.intent.with_client_order_id(
            make_client_order_id(STRATEGY_NAME, symbol, ctx.session_date, 1)
        )
        print(">>> 전송하지 않았습니다 (--execute 없음). 보낼 내용:")
        print(f"    {order_body(preview)}")
        return EXIT_OK

    if not _confirm(signal, args.yes):
        return EXIT_REFUSED

    trading = build_trading_api(
        config, mode=TradingMode.LIVE, account_seq=service.account.resolve_account_seq()
    )
    executor = OrderExecutor(
        trading, store, price_limits=ctx.price_limits, session_date=ctx.session_date
    )
    record = executor.submit(decision.intent)

    print()
    print(f">>> 주문 결과: {record.status}")
    print(f"    client_order_id: {record.client_order_id}")
    if record.order_id:
        print(f"    브로커 order_id: {record.order_id}")
    if record.error_code:
        print(f"    오류 코드: {record.error_code}")
    if record.detail:
        print(f"    {record.detail}")
    if record.duplicate:
        print("    이미 기록된 주문이라 재발주하지 않았습니다.")

    print()
    print(">>> 체결 확인은 다음 명령으로:")
    print("    python scripts/live_check.py --settle")
    return EXIT_OK


def _currency_of(ctx, symbol):
    """USD unless the account already holds this symbol in something else."""
    position = ctx.position(symbol)
    if position is not None and position.currency:
        return position.currency
    return "USD"


def main():
    try:
        return run()
    except ConfigFileMissing as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except TossError as exc:
        print(f"ERROR: 토스 API 오류: {exc}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
