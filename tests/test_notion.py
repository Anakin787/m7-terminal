"""Report target resolution: a database and an ordinary page both work.

Notion gives both kinds of target a similar URL, and only a database link
carries the ?v= view parameter that distinguishes them - which is easy to lose
when copying a link. Passing a page id to databases.retrieve returns a 404
that reads exactly like a missing integration connection, so the reporter
probes for both.
"""

from decimal import Decimal

import pytest

from src.models import SOURCE_MANUAL, PortfolioSnapshot, Position
from src.notion import (
    PARENT_DATABASE,
    PARENT_PAGE,
    NotionReporter,
    _markdown_to_blocks,
    resolve_parent,
)

DATABASE_ID = "1a30084c658a4e78944080096889dbfb"


class FakeAPIError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


class FakeEndpoint:
    def __init__(self, result=None, error_code=None):
        self.result = result
        self.error_code = error_code

    def retrieve(self, **kwargs):
        if self.error_code:
            raise FakeAPIError(self.error_code)
        return self.result


class FakePages:
    def __init__(self, retrieve_result=None, error_code=None):
        self._retrieve = FakeEndpoint(retrieve_result, error_code)
        self.created = None

    def retrieve(self, **kwargs):
        return self._retrieve.retrieve(**kwargs)

    def create(self, **kwargs):
        self.created = kwargs
        return {"id": "new-page-id", "url": "https://notion.so/new-page-id"}


class FakeBlockChildren:
    def __init__(self):
        self.appended = []

    def append(self, **kwargs):
        self.appended.append(kwargs)


class FakeBlocks:
    def __init__(self):
        self.children = FakeBlockChildren()


class FakeClient:
    def __init__(self, databases=None, pages=None):
        self.blocks = FakeBlocks()
        self.databases = databases or FakeEndpoint(error_code="object_not_found")
        self.pages = pages or FakePages(error_code="object_not_found")


@pytest.fixture(autouse=True)
def patch_api_error(monkeypatch):
    import notion_client.errors

    monkeypatch.setattr(notion_client.errors, "APIResponseError", FakeAPIError)


@pytest.fixture
def snapshot():
    return PortfolioSnapshot(
        positions=[
            Position(
                symbol="IONX", name="IONX", market_country="US", currency="USD",
                quantity=Decimal("325"), last_price=Decimal("30.31"),
                avg_purchase_price=Decimal("37.8846"), source=SOURCE_MANUAL,
                profit_rate=Decimal("-0.1999"),
            )
        ],
        exchange_rate=Decimal("1398"),
        total_krw=Decimal("13771349"),
        purchase_krw=Decimal("18050856"),
        profit_krw=Decimal("-4279507"),
        profit_rate=Decimal("-0.2371"),
    )


def config_for(target_id=DATABASE_ID):
    return {"token": "ntn_test", "database_id": target_id}


def test_database_target_is_detected():
    client = FakeClient(databases=FakeEndpoint({"title": [{"plain_text": "리포트 DB"}]}))

    assert resolve_parent(client, DATABASE_ID) == (PARENT_DATABASE, "리포트 DB")


def test_page_target_is_detected():
    """A /p/ link with no ?v= is a page, and must not be reported as missing."""
    client = FakeClient(
        pages=FakePages({"properties": {"title": {"type": "title",
                                                  "title": [{"plain_text": "StockLens-AI"}]}}})
    )

    assert resolve_parent(client, DATABASE_ID) == (PARENT_PAGE, "StockLens-AI")


def test_unknown_id_explains_the_connection_step():
    kind, detail = resolve_parent(FakeClient(), DATABASE_ID)

    assert kind is None
    assert "Connections" in detail


def test_report_into_a_database_sets_report_and_date(snapshot):
    pages = FakePages()
    client = FakeClient(databases=FakeEndpoint({"title": []}), pages=pages)

    NotionReporter(config_for(), client=client).create_report(snapshot, {})

    assert pages.created["parent"] == {"database_id": DATABASE_ID}
    assert "Report" in pages.created["properties"]
    assert "Date" in pages.created["properties"]


def test_report_into_a_page_uses_only_a_title(snapshot):
    """A child page has no Report/Date columns; sending them would 400."""
    pages = FakePages({"properties": {}})
    client = FakeClient(pages=pages)

    NotionReporter(config_for(), client=client).create_report(snapshot, {})

    assert pages.created["parent"] == {"page_id": DATABASE_ID}
    assert list(pages.created["properties"]) == ["title"]


def test_report_returns_the_created_page_url(snapshot):
    pages = FakePages({"properties": {}})
    result = NotionReporter(config_for(), client=FakeClient(pages=pages)).create_report(
        snapshot, {}
    )

    assert result["page_id"] == "new-page-id"
    assert result["url"] == "https://notion.so/new-page-id"


def test_unresolvable_target_raises_before_building_the_page(snapshot):
    with pytest.raises(ValueError, match="Connections"):
        NotionReporter(config_for(), client=FakeClient()).create_report(snapshot, {})


def test_parent_is_probed_only_once(snapshot):
    class CountingDatabases(FakeEndpoint):
        calls = 0

        def retrieve(self, **kwargs):
            CountingDatabases.calls += 1
            return {"title": []}

    client = FakeClient(databases=CountingDatabases(), pages=FakePages())
    reporter = NotionReporter(config_for(), client=client)
    reporter.create_report(snapshot, {})
    reporter.create_report(snapshot, {})

    assert CountingDatabases.calls == 1


def test_summary_includes_invested_amount(snapshot):
    pages = FakePages({"properties": {}})
    NotionReporter(config_for(), client=FakeClient(pages=pages)).create_report(snapshot, {})

    text = str(pages.created["children"])
    assert "Invested" in text
    assert "18,050,856" in text


def test_ai_comment_markdown_is_parsed_into_blocks(snapshot):
    """A raw markdown string, dumped into one Notion text block, renders the
    literal ``**``/``*`` markers instead of bold/italic - Notion's API does not
    parse markdown the way typing into the editor does."""
    pages = FakePages({"properties": {}})
    comment = "**1. Market Outlook**\nSome text with **bold** and *italic* words."

    NotionReporter(config_for(), client=FakeClient(pages=pages)).create_report(
        snapshot, {}, ai_comment=comment
    )

    children = pages.created["children"]
    types = [block["type"] for block in children]
    assert "heading_3" in types  # "**1. Market Outlook**" -> a real heading
    for block in children:
        text = str(block)
        assert "**" not in text
        assert "*italic*" not in text


def test_markdown_bold_and_italic_become_annotations():
    blocks = _markdown_to_blocks("Plain **bold** and *italic* text.")

    rich_text = blocks[0]["paragraph"]["rich_text"]
    contents = [(span["text"]["content"], span.get("annotations", {})) for span in rich_text]
    assert ("bold", {"bold": True}) in contents
    assert ("italic", {"italic": True}) in contents


def test_markdown_nested_bullets_become_children():
    blocks = _markdown_to_blocks("* Top item\n    * Nested item")

    assert len(blocks) == 1
    top = blocks[0]["bulleted_list_item"]
    assert top["rich_text"][0]["text"]["content"] == "Top item"
    nested = top["children"][0]["bulleted_list_item"]
    assert nested["rich_text"][0]["text"]["content"] == "Nested item"


def test_the_universe_review_separates_what_was_applied_from_what_was_only_suggested(snapshot):
    """A reader must never have to work out which AI list already changed the
    engine's behaviour and which is waiting on them."""
    from src.universe_review import Candidate, UniverseReview, Veto

    pages = FakePages({"properties": {}})
    review = UniverseReview(
        vetoes=(
            Veto(symbol="INTC", category="trading_halt", reason="거래정지", evidence="halted"),
        ),
        candidates=(Candidate(symbol="ASML", name="ASML", reason="장비 노출"),),
    )

    NotionReporter(config_for(), client=FakeClient(pages=pages)).create_report(
        snapshot, {}, universe_review=review
    )

    text = str(pages.created["children"])
    assert "자동 적용됨" in text and "INTC" in text
    assert "자동 반영 안 됨" in text and "ASML" in text


def test_an_empty_universe_review_adds_no_section(snapshot):
    from src.universe_review import UniverseReview

    pages = FakePages({"properties": {}})
    NotionReporter(config_for(), client=FakeClient(pages=pages)).create_report(
        snapshot, {}, universe_review=UniverseReview()
    )

    assert "AI Universe Review" not in str(pages.created["children"])


def test_blocks_past_the_per_request_limit_are_appended(snapshot):
    """Notion rejects more than 100 children in one request; the rest follow."""
    pages = FakePages({"properties": {}})
    client = FakeClient(pages=pages)
    news = {"general": [{"title": f"n{i}", "link": "https://x"} for i in range(230)]}

    NotionReporter(config_for(), client=client).create_report(snapshot, news)

    appended = client.blocks.children.appended
    assert len(pages.created["children"]) == 100
    assert all(len(call["children"]) <= 100 for call in appended)
    assert all(call["block_id"] == "new-page-id" for call in appended)
    assert len(appended) == 2
