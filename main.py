"""Daily report entry point.

Exits non-zero on failure so a scheduled run surfaces problems instead of
quietly reporting success, which the previous version did.
"""

import sys

# The Windows console is often cp949, which cannot encode characters like an
# em dash. Without this, a diagnostic script dies on its own status message.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass

from src.analyst import Analyst
from src.audit import record_config_changes, review_entries
from src.config import ConfigFileMissing, load_config, require_config_file
from src.news import NewsFetcher, portfolio_keywords
from src.notion import NotionReporter
from src.pipeline import PortfolioService, apply_name_overrides
from src.store.repo import Store
from src.strategy.loader import load_strategies
from src.strategy.universe import BUCKET_GROWTH, KIND_INDEX_ETF, parse_universe
from src.toss.errors import TossError
from src.universe_review import UniverseReviewer


def _bucket_allocation(config, snapshot):
    """Where the portfolio sits against the active strategy's bucket plan.

    Returns None when the strategy has no buckets - momentum_dca has no such
    plan, and printing a table of empty targets next to it would invent one.
    Never raises: a report that fails because an extra section could not be
    built is worse than a report without that section.
    """
    try:
        strategies = load_strategies(config.trading)
        targets = next(
            (
                s.params.weights
                for s in strategies
                if isinstance(getattr(getattr(s, "params", None), "weights", None), dict)
            ),
            None,
        )
        if not targets:
            return None
        return parse_universe(config.trading.universe).bucket_allocation(
            snapshot, targets=targets
        )
    except Exception as exc:  # noqa: BLE001 - see docstring
        print(f"경고: 버킷 배분을 계산하지 못했습니다 ({exc})")
        return None


def _review_universe(config, snapshot, news_data, store):
    """Ask the model which universe members to pause and what to consider.

    Returns None when there is nothing to report, which is the ordinary
    outcome: on a day with no delisting or halt in the headlines, an empty
    veto list is the correct answer, not a failure.
    """
    reviewer = UniverseReviewer(config)
    if not reviewer.enabled:
        return None

    print(">>> AI universe review...")
    held = [p.symbol for p in snapshot.positions if p.symbol]
    universe = parse_universe(config.trading.universe)
    review = reviewer.review(
        universe.symbols(),
        held,
        news_data,
        growth_symbols=[i.symbol for i in universe.by_bucket(BUCKET_GROWTH)],
    )
    if review.error:
        print(f"    건너뜀: {review.error}")
        return None

    if not review.is_empty:
        store.save_universe_review(review, ttl_days=config.analyst.veto_ttl_days)
        store.save_audit_entries(review_entries(review))
    for veto in review.vetoes:
        print(f"    보류: {veto.symbol} [{veto.category}] {veto.reason}")
    if review.candidates:
        print(
            "    편입 후보(검토용): "
            + ", ".join(c.symbol for c in review.candidates)
        )
    if review.growth_candidates:
        print(
            "    미래성장 후보(검토용): "
            + ", ".join(c.symbol for c in review.growth_candidates)
        )
    if not review.vetoes:
        print("    보류할 종목 없음 (정상)")
    return review


def run():
    print(">>> Starting Financial Reporter...")

    # The holdings live in config.yaml, so a missing one is not "no settings",
    # it is "no portfolio" - and that records as a real 0 KRW day.
    require_config_file()
    config = load_config()

    # 1. Portfolio: Toss account (automatic) + manual entries for holdings
    #    kept at other brokers, priced through the Toss market data API.
    print(">>> Fetching portfolio (Toss Open API)...")
    store = Store()
    service = PortfolioService(config)
    try:
        snapshot = service.snapshot()
    finally:
        service.close()

    # Display names edited in the dashboard apply to the report too, so a
    # ticker reads the same in both places.
    apply_name_overrides(snapshot, store.symbol_names())

    print(
        f"Summary: {snapshot.total_krw:,.0f} KRW "
        f"(P&L {snapshot.profit_krw:+,.0f}, {snapshot.profit_rate * 100:+.2f}%)"
    )
    if snapshot.daily_profit_krw:
        print(
            f"Today:   {snapshot.daily_profit_krw:+,.0f} KRW "
            f"({snapshot.daily_profit_rate * 100:+.2f}%)"
        )
    if snapshot.warnings:
        print(f"Warnings: {len(snapshot.warnings)} -> {'; '.join(snapshot.warnings)}")

    # 2. Persist before anything that can fail on a third-party service. The
    #    Portfolio Value chart cannot be backfilled, so the snapshot is worth
    #    keeping even if Notion or Gemini is down.
    # 2b. Audit: did anyone change the universe, the strategy or its limits
    #     since the last run? Detected, not declared - nobody has to remember
    #     to write it down.
    for entry in record_config_changes(
        store, config.trading, parse_universe(config.trading.universe)
    ):
        print(f">>> 설정 변경 감지: [{entry['category']}] {entry['summary']}")

    ts = store.save_snapshot(snapshot)
    print(f">>> Snapshot saved ({ts}, total {store.snapshot_count()} rows)")

    # 3. News - the configured macro keywords, plus one per currently held
    #    stock, so the report also surfaces news about the actual portfolio
    #    and not only the general economy.
    print(">>> Fetching news...")
    keywords = list(config.news_keywords)
    # ETFs and commodity funds get no section of their own; the user found
    # asset-class headlines (Treasuries, gold, a sector) to be noise.
    funds = [
        i.symbol
        for i in parse_universe(config.trading.universe)
        if i.kind == KIND_INDEX_ETF
    ]
    for name in portfolio_keywords(snapshot, fund_symbols=funds):
        if name not in keywords:
            keywords.append(name)
    news_data = NewsFetcher({"news": {"keywords": keywords}}).fetch_daily_news()
    print(
        f"Fetched {len(news_data.get('general', []))} general news items, "
        f"{len(news_data.get('keywords', {}))} keyword sections ({', '.join(keywords)})."
    )

    # 4. AI analysis
    print(">>> AI Analyst is thinking...")
    ai_comment = Analyst(config).analyze_portfolio(snapshot, news_data)

    # 4b. AI universe review. Runs on the same headlines the analyst just
    #     read, and is the one AI output with teeth: an accepted veto pauses
    #     new buys of that symbol until it expires. Candidates are advisory
    #     and stop at the report. Failure here degrades the report, never the
    #     trading run - active vetoes simply stay whatever they already were.
    review = _review_universe(config, snapshot, news_data, store)

    # 5. Notion
    if not config.notion.is_configured:
        print("ERROR: Please set your valid Notion Token in config.yaml")
        return 1

    print(">>> Reporting to Notion...")
    report = NotionReporter(config).create_report(
        snapshot,
        news_data,
        ai_comment,
        universe_review=review,
        bucket_allocation=_bucket_allocation(config, snapshot),
    )
    if report.get("page_id"):
        store.save_report(
            report["page_id"],
            title=report.get("title"),
            url=report.get("url"),
            ai_comment=ai_comment,
            ts=ts,
        )

    print(">>> Done.")
    return 0


def main():
    try:
        return run()
    except ConfigFileMissing as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 3
    except TossError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - top level guard for the batch job
        print(f"ERROR: 예기치 못한 오류 - {exc}", file=sys.stderr)
        import traceback

        traceback.print_exc()
        return 3


if __name__ == "__main__":
    sys.exit(main())
