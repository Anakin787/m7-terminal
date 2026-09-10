"""Trading engine entry point. PAPER by default.

Separate from main.py because the two batches fail differently: a report that
does not run costs a chart point, and a trading run that does not run costs a
trade - or, worse, runs twice. They also want different schedules.

    python trade.py               # PAPER - reads the market, sends nothing
    python trade.py --dry-run     # risk gate only, nothing written
    python trade.py --reconcile   # poll open LIVE orders, record fills, arm OCO brackets
    python trade.py --live        # refused until step [10] opens it
"""

import argparse
import sys
from datetime import date, timedelta

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass

from src.config import ConfigFileMissing, load_config, require_config_file
from src.audit import record_config_changes
from src.data.cache import BarCache
from src.data.loader import HistoryLoader
from src.data.yahoo import YahooBarSource
from src.execution.context import build_context
from src.execution.executor import OrderExecutor
from src.execution.reconciler import Reconciler
from src.execution.risk import RiskGate
from src.pipeline import PortfolioService
from src.store.repo import Store
from src.strategy.loader import load_strategies
from src.strategy.universe import Universe, parse_universe
from src.toss.errors import TossError
from src.toss.trading import TradingMode, build_trading_api

EXIT_OK = 0
EXIT_DISABLED = 1
EXIT_TOSS_ERROR = 2
EXIT_UNEXPECTED = 3
EXIT_LIVE_BLOCKED = 4

#: Long enough for a 252-day momentum lookback plus its skip and a trend SMA,
#: with room to spare. Longer than any strategy needs costs one extra request
#: per symbol per run, not correctness.
HISTORY_LOOKBACK_DAYS = 400


def _strategy_symbols(strategies):
    """Every symbol any loaded strategy might want priced or ranked.

    Read from each strategy's own ``universe``/``params.benchmark`` rather
    than from config directly - a strategy with no such attributes (one that
    does not use the universe module at all) is simply skipped, not an error.
    """
    symbols = set()
    for strategy in strategies:
        universe = getattr(strategy, "universe", None)
        if isinstance(universe, Universe):
            symbols.update(universe.symbols())
        benchmark = getattr(getattr(strategy, "params", None), "benchmark", None)
        if benchmark:
            symbols.add(benchmark)
    return symbols


def _load_history(config, symbols):
    """Cached daily bars for every symbol a strategy might need.

    A data outage here must degrade the run to "no signals" - a strategy with
    an empty ``history`` simply produces nothing - rather than crash the
    trading batch. yfinance itself, and the network under it, can fail in
    more ways than this module can enumerate, so the catch is deliberately
    broad.
    """
    if not symbols:
        return {}
    try:
        cache = BarCache()
        loader = HistoryLoader(cache, source=YahooBarSource(), offline=False)
        end = date.today()
        start = end - timedelta(days=HISTORY_LOOKBACK_DAYS)
        return loader.load(sorted(symbols), start, end)
    except Exception as exc:  # noqa: BLE001 - see docstring
        print(f"!!! 과거 시세 로딩 실패 - 히스토리 없이 진행합니다: {exc}")
        return {}


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="M7 Terminal trading engine")
    parser.add_argument(
        "--live",
        action="store_true",
        help="실계좌 주문. 현재 단계에서는 거부됩니다 (설계 [10]에서 별도로 엽니다).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="리스크 게이트까지만 실행하고 DB에 아무것도 쓰지 않습니다.",
    )
    parser.add_argument(
        "--reconcile",
        action="store_true",
        help="새 신호를 평가하지 않고, 미체결 LIVE 주문의 체결을 확인하고 OCO를 등록합니다.",
    )
    return parser.parse_args(argv)


def _banner(mode, dry_run):
    """State the mode before anything happens.

    Design section 7 lists PAPER/LIVE confusion as a live risk, and the
    cheapest mitigation is that every run says out loud which one it is.
    """
    label = "DRY-RUN (기록 없음)" if dry_run else mode.value.upper()
    print("=" * 56)
    print(f"  M7 Terminal · 매매 엔진 · 모드: {label}")
    if mode is TradingMode.PAPER:
        print("  주문은 전송되지 않습니다. 쓰기 권한 없는 클라이언트를 사용합니다.")
    print("=" * 56)


def run(argv=None):
    args = parse_args(argv)

    if args.live:
        # Refused rather than merely discouraged. Step [10] is where LIVE
        # opens, gated on its own separate validation (minimum 1-share real
        # trade) - the reconciler and OCO bracket existing is necessary but
        # not sufficient for that step to be considered done.
        print(
            "ERROR: --live는 아직 열려 있지 않습니다.\n"
            "       설계 6절 [10]에서 최소 수량 1주로 별도 검증한 뒤 엽니다.",
            file=sys.stderr,
        )
        return EXIT_LIVE_BLOCKED

    # Strategies, limits and the universe all come from config.yaml. Running
    # without it would not trade - it would silently do nothing and look fine.
    require_config_file()
    config = load_config()

    if args.reconcile:
        return _reconcile(config)

    mode = TradingMode.PAPER

    if not config.trading.enabled:
        print("매매가 비활성화되어 있습니다. config.yaml의 trading.enabled를 켜세요.")
        return EXIT_DISABLED

    strategies = load_strategies(config.trading)
    if not strategies:
        print("등록된 전략이 없습니다. config.yaml의 trading.strategies를 채우세요.")
        return EXIT_DISABLED

    _banner(mode, args.dry_run)
    print(f">>> 전략 {len(strategies)}개: {', '.join(s.name for s in strategies)}")

    store = Store()
    # Same fingerprint source as the report pipeline (config, not the loaded
    # strategy objects), so whichever process runs first records the change
    # and the other sees nothing left to record. Skipped in --dry-run, which
    # promises to write nothing.
    if not args.dry_run:
        for entry in record_config_changes(
            store, config.trading, parse_universe(config.trading.universe)
        ):
            print(f">>> 설정 변경 감지: [{entry['category']}] {entry['summary']}")

    service = PortfolioService(config)
    try:
        universe_symbols = _strategy_symbols(strategies)
        if universe_symbols:
            print(f">>> 과거 시세 준비 중... ({sorted(universe_symbols)})")
        history = _load_history(config, universe_symbols)

        print(">>> 컨텍스트 수집 중 (시세·잔고·장 운영시간)...")
        ctx = build_context(
            service,
            store,
            symbols=sorted(universe_symbols),
            kill_switch_path=config.trading.kill_switch_path,
            history=history,
            recent=store.recent_signals(limit=50),
        )
        if ctx.kill_switch:
            print("!!! KILL_SWITCH가 활성화되어 있습니다. 모든 신호가 거부됩니다.")
        print(
            f"    보유 {len(ctx.positions)}종목 · 시세 {len(ctx.prices)}건 · "
            f"오늘 주문 {ctx.daily_usage.order_count}건"
        )
        # Named, not counted: a paused symbol changes what this run can do,
        # and "2종목 보류" would leave the reader to guess which two.
        for symbol, reason in sorted((ctx.blocked_symbols or {}).items()):
            print(f"    보류(AI): {symbol} — {reason}")

        signals = []
        for strategy in strategies:
            produced = strategy.evaluate(ctx) or []
            print(f"    {strategy.name}: 신호 {len(produced)}건")
            signals.extend(produced)

        if not signals:
            print(">>> 신호가 없습니다.")
            return EXIT_OK

        gate = RiskGate(config.trading.risk_limits())
        approved, rejections = [], {}
        # Batch, not one-at-a-time: the daily limits are limits on the *day*,
        # and a run's own earlier approvals are part of that day even though
        # ctx was read before any of them existed.
        for decision in gate.evaluate_batch(signals, ctx):
            signal_id = None if args.dry_run else store.save_decision(decision)
            if decision.approved:
                approved.append((decision.intent, signal_id))
            else:
                rule = decision.rejection.rule
                rejections.setdefault(rule, []).append(decision.rejection.detail)

        _report_rejections(rejections)

        if args.dry_run:
            print(f">>> DRY-RUN: 승인 {len(approved)}건. 아무것도 기록하지 않았습니다.")
            return EXIT_OK

        if not approved:
            return EXIT_OK

        # Reuse the account sequence the service already resolved: ACCOUNT
        # allows one request per second, and re-resolving it here would spend
        # that budget to learn something we know.
        trading = build_trading_api(
            config, mode=mode, account_seq=service.account.resolve_account_seq()
        )
        executor = OrderExecutor(
            trading, store, price_limits=ctx.price_limits
        )
        for intent, signal_id in approved:
            record = executor.submit(intent, signal_id=signal_id)
            note = " (중복, 재발주 안 함)" if record.duplicate else ""
            print(
                f"    {record.client_order_id}: {record.status}{note}"
                f"{' · ' + record.detail if record.detail and not record.duplicate else ''}"
            )
        print(f">>> 신호 {len(signals)}건 · 승인 {len(approved)}건 · 거부 {len(signals) - len(approved)}건")
    finally:
        service.close()

    return EXIT_OK


def _reconcile(config):
    store = Store()
    trading = build_trading_api(config, mode=TradingMode.LIVE)
    reconciler = Reconciler(
        trading,
        store,
        oco_expire_days=config.trading.oco_expire_days,
        oco_stop_loss_slippage=config.trading.oco_stop_loss_slippage,
    )

    print(">>> 미체결 LIVE 주문 조회 중...")
    results = reconciler.run()
    if not results:
        print(">>> 조회할 미체결 주문이 없습니다.")
        return EXIT_OK

    for line in results:
        print(f"    {line}")
    return EXIT_OK


def _report_rejections(rejections):
    if not rejections:
        return
    print(">>> 거부:")
    for rule, details in sorted(rejections.items()):
        print(f"    [{rule}] {len(details)}건 - {details[0]}")


def main(argv=None):
    try:
        return run(argv)
    except ConfigFileMissing as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_UNEXPECTED
    except TossError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_TOSS_ERROR
    except Exception as exc:  # noqa: BLE001 - top level guard for the batch job
        print(f"ERROR: 예기치 못한 오류 - {exc}", file=sys.stderr)
        import traceback

        traceback.print_exc()
        return EXIT_UNEXPECTED


if __name__ == "__main__":
    sys.exit(main())
