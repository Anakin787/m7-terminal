"""portfolio_keywords() and NewsFetcher's keyword fan-out."""

import urllib.parse
from decimal import Decimal
from types import SimpleNamespace

from src.models import SOURCE_TOSS, PortfolioSnapshot, Position
from src.news import NewsFetcher, portfolio_keywords


def position(symbol, name, underlying=None):
    return Position(
        symbol=symbol,
        name=name,
        market_country="KR",
        currency="KRW",
        quantity=Decimal("1"),
        last_price=Decimal("1"),
        avg_purchase_price=Decimal("1"),
        source=SOURCE_TOSS,
        underlying=underlying,
    )


def test_portfolio_keywords_lists_distinct_held_names():
    snapshot = PortfolioSnapshot(positions=[position("005930", "삼성전자"), position("AAPL", "Apple Inc.")])
    assert portfolio_keywords(snapshot) == ["삼성전자", "Apple Inc."]


def test_portfolio_keywords_dedupes_and_skips_blank_names():
    snapshot = PortfolioSnapshot(
        positions=[position("005930", "삼성전자"), position("005930", "삼성전자"), position(None, "")]
    )
    assert portfolio_keywords(snapshot) == ["삼성전자"]


def test_portfolio_keywords_on_an_empty_portfolio():
    assert portfolio_keywords(PortfolioSnapshot(positions=[])) == []


def test_portfolio_keywords_prefers_the_declared_underlying():
    # A leveraged/single-stock product's own listed name searches poorly;
    # the underlying company's name is what a general news search wants.
    snapshot = PortfolioSnapshot(
        positions=[position("TSLL", "Direxion Daily TSLA Bull 2X Shares", underlying="TSLA")]
    )
    assert portfolio_keywords(snapshot) == ["TSLA"]


def test_portfolio_keywords_falls_back_to_name_without_an_underlying():
    snapshot = PortfolioSnapshot(positions=[position("005930", "삼성전자")])
    assert portfolio_keywords(snapshot) == ["삼성전자"]


class FakeEntry:
    def __init__(self, title, link, published, source_title):
        self.title = title
        self.link = link
        self.published = published
        self.source = {"title": source_title}


def test_fetch_daily_news_returns_a_section_per_keyword(monkeypatch):
    calls = []

    def fake_parse(url):
        calls.append(url)
        return SimpleNamespace(
            entries=[FakeEntry("한 종목 기사", "https://example.com/1", "2026-08-27", "Test Wire")]
        )

    monkeypatch.setattr("src.news.feedparser.parse", fake_parse)

    fetcher = NewsFetcher({"news": {"keywords": ["삼성전자", "Apple Inc."]}})
    result = fetcher.fetch_daily_news()

    # One call for "general" (경제) plus one per keyword.
    assert len(calls) == 3
    assert set(result["keywords"]) == {"삼성전자", "Apple Inc."}
    assert result["keywords"]["삼성전자"][0]["title"] == "한 종목 기사"
    assert result["keywords"]["삼성전자"][0]["source"] == "Test Wire"


def test_a_ticker_search_drops_headlines_that_are_not_about_the_security(monkeypatch):
    """SHY is a Treasury ETF; "Super Shy" is a K-pop song."""
    calls = []
    titles = [
        "뉴진스 '슈퍼 샤이', 스포티파이 9억 스트리밍",
        "뉴진스, 또 일냈다..'Super Shy' 스포티파이 9억 스트리밍 돌파",
        "AI 생성 논란 호러소설 '샤이 걸(Shy Girl)' 출판 취소",
        "단기 국채 ETF SHY 자금 유입 늘어",
    ]

    def fake_parse(url):
        calls.append(url)
        return SimpleNamespace(
            entries=[FakeEntry(t, "https://example.com", "2026-10-06", "Wire") for t in titles]
        )

    monkeypatch.setattr("src.news.feedparser.parse", fake_parse)

    result = NewsFetcher({"news": {"keywords": ["SHY", "삼성전자"]}}).fetch_daily_news()

    assert [i["title"] for i in result["keywords"]["SHY"]] == ["단기 국채 ETF SHY 자금 유입 늘어"]
    assert "ETF" in urllib.parse.unquote(calls[1])
    # A name that is not a bare ticker is searched and kept as before.
    assert len(result["keywords"]["삼성전자"]) == 4
