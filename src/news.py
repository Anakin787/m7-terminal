import re
import urllib.parse
from datetime import datetime

import feedparser

# A bare ticker is a common word or a name to a news search: SHY (a Treasury
# ETF) returns "Super Shy" K-pop headlines. Searches for one are steered to
# markets and every headline must pass the check below before it is reported.
_TICKER = re.compile(r"[A-Z][A-Z.\-]{0,5}")
_FINANCE_TERMS = (
    "주가", "주식", "ETF", "증시", "투자", "종목", "배당", "실적", "상장", "펀드",
    "국채", "금리", "매수", "매도", "시총", "시가총액", "나스닥", "월가", "목표가",
    "stock", "shares", "etf", "nasdaq", "treasury", "earnings", "dividend",
)


def portfolio_keywords(snapshot):
    """Distinct news-search keywords for currently held positions, in order.

    Feeds the report's news search with the account's actual stocks, not
    just the static macro keywords in config - "news about what I hold", not
    only "news about the economy in general".

    A position's ``underlying`` wins when set (a leveraged or single-stock
    product declares the one company it actually tracks -
    config.yaml's ``portfolio.manual[].underlying`` - e.g. TSLL -> "TSLA");
    searching a fund's full listed name ("DIREXION DAILY TSLA BULL 2X
    SHARES") returns almost nothing on a general news search, where the
    underlying company's own name does. Otherwise the display name is used
    rather than the ticker: "삼성전자" surfaces far more on Google News than
    the bare symbol "005930" would.
    """
    seen = []
    for position in getattr(snapshot, "positions", None) or []:
        keyword = getattr(position, "underlying", None) or getattr(position, "name", None) or ""
        keyword = keyword.strip()
        if keyword and keyword not in seen:
            seen.append(keyword)
    return seen


def _is_about_the_ticker(title, ticker):
    """The headline names the ticker and reads as a market story."""
    lowered = title.lower()
    return ticker.lower() in lowered and any(t.lower() in lowered for t in _FINANCE_TERMS)


class NewsFetcher:
    def __init__(self, config):
        self.keywords = config.get('news', {}).get('keywords', [])

    def fetch_daily_news(self):
        """
        Fetches general economic news and keyword-specific news.
        Returns a dictionary or list of news items.
        """
        results = {
            "general": self._fetch_google_news("경제"),
            "keywords": {}
        }

        for keyword in self.keywords:
            if _TICKER.fullmatch(keyword):
                found = self._fetch_google_news(f"{keyword} 주가 OR ETF OR 주식")
                results["keywords"][keyword] = [
                    item for item in found if _is_about_the_ticker(item["title"], keyword)
                ]
            else:
                results["keywords"][keyword] = self._fetch_google_news(keyword)

        return results

    def _fetch_google_news(self, query):
        encoded_query = urllib.parse.quote(query)
        # Google News RSS for Korea
        url = f"https://news.google.com/rss/search?q={encoded_query}&hl=ko&gl=KR&ceid=KR:ko"
        
        feed = feedparser.parse(url)
        news_items = []
        
        # Get top 5 items
        for entry in feed.entries[:5]:
            news_items.append({
                "title": entry.title,
                "link": entry.link,
                "published": entry.published,
                "source": entry.source.get('title', 'Unknown')
            })
            
        return news_items

if __name__ == "__main__":
    # Test run
    dummy_config = {"news": {"keywords": ["삼성전자", "환율"]}}
    fetcher = NewsFetcher(dummy_config)
    news = fetcher.fetch_daily_news()
    import json
    print(json.dumps(news, indent=2, ensure_ascii=False))
