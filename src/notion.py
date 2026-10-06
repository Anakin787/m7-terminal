import re
from datetime import datetime
from decimal import Decimal

from src.models import ZERO
from src.strategy.universe import (
    BUCKET_CORE,
    BUCKET_GROWTH,
    BUCKET_SAFE,
    UNMANAGED,
)

from notion_client import Client

#: Property names used when the report target is a database.
TITLE_PROP = "Report"
DATE_PROP = "Date"

#: Columns a database target must have. Only checked for diagnostics -
#: a page target has no properties beyond its title.
REQUIRED_PROPS_HINT = {TITLE_PROP: "title", DATE_PROP: "date"}

#: Shown in the report instead of the bare bucket names.
_MAX_BLOCKS_PER_REQUEST = 100

_BUCKET_LABELS = {
    BUCKET_SAFE: "🛡 안전자산",
    BUCKET_CORE: "📊 일반",
    BUCKET_GROWTH: "🚀 미래성장",
    UNMANAGED: "❔ 전략 외 보유",
}

PARENT_DATABASE = "database"
PARENT_PAGE = "page"


def _fmt_krw(value):
    if value is None:
        return "-"
    return f"{Decimal(value).quantize(Decimal('1')):,} KRW"


def _fmt_pct(rate):
    """Toss reports rates as ratios (0.1179), reports show percentages."""
    if rate is None:
        return "-"
    return f"{Decimal(rate) * 100:+.2f}%"


def _fmt_signed(value):
    if value is None:
        return "-"
    amount = Decimal(value).quantize(Decimal("1"))
    return f"{amount:+,} KRW"


def database_properties(client, database_id):
    """The column schema of a database, on either Notion API version.

    Notion 2025-09-03 moved a database's columns onto its *data sources*, so
    ``databases.retrieve`` stopped returning ``properties`` - it now returns a
    ``data_sources`` list instead. Reading only the old shape makes a healthy
    database look like it has no columns at all, which reads exactly like a
    misnamed Report/Date column. Ask the data source when there is one, and
    fall back to the pre-2025 shape otherwise.
    """
    database = client.databases.retrieve(database_id=database_id)
    properties = database.get("properties")
    if properties:
        return properties

    sources = database.get("data_sources") or []
    if not sources:
        return {}
    source = client.request(path=f"data_sources/{sources[0]['id']}", method="GET")
    return source.get("properties") or {}


def resolve_parent(client, target_id):
    """Work out whether ``target_id`` is a database or an ordinary page.

    Notion's UI gives both the same style of URL, and only a database link
    carries a ``?v=`` view parameter - which is easy to lose when copying.
    Probing here means either kind of link works, instead of a page id
    failing with a 404 that reads exactly like a missing integration
    connection.

    Returns (kind, title) or (None, error message).
    """
    from notion_client.errors import APIResponseError

    try:
        database = client.databases.retrieve(database_id=target_id)
        return PARENT_DATABASE, _plain_title(database.get("title"))
    except APIResponseError as exc:
        if getattr(exc, "code", "") not in ("object_not_found", "validation_error"):
            raise

    try:
        page = client.pages.retrieve(page_id=target_id)
    except APIResponseError:
        return None, (
            "해당 ID의 데이터베이스도 페이지도 찾을 수 없습니다. "
            "ID가 맞는지, 그리고 그 페이지에서 [...] > Connections 로 "
            "integration을 연결했는지 확인하세요."
        )

    title_prop = next(
        (
            value
            for value in (page.get("properties") or {}).values()
            if value.get("type") == "title"
        ),
        {},
    )
    return PARENT_PAGE, _plain_title(title_prop.get("title"))


def _plain_title(rich_text):
    return "".join(part.get("plain_text", "") for part in (rich_text or []))


class NotionReporter:
    def __init__(self, config, client=None):
        # Accepts either the AppConfig.notion dataclass or a raw mapping.
        notion_cfg = getattr(config, "notion", config)
        self.token = _get(notion_cfg, "token")
        self.database_id = _get(notion_cfg, "database_id")
        self.client = client or Client(auth=self.token)
        self.title_prefix = _get(notion_cfg, "page_title_prefix", "Financial Report")

        self.title_prop_name = TITLE_PROP
        self.date_prop_name = DATE_PROP
        self._parent_kind = None

    def parent_kind(self):
        """Cache the probe so a report run only checks once."""
        if self._parent_kind is None:
            kind, detail = resolve_parent(self.client, self.database_id)
            if kind is None:
                raise ValueError(detail)
            self._parent_kind = kind
        return self._parent_kind

    def _parent(self):
        if self.parent_kind() == PARENT_DATABASE:
            return {"database_id": self.database_id}
        return {"page_id": self.database_id}

    def _properties(self, title):
        """Databases carry Report/Date columns; a child page has only a title."""
        if self.parent_kind() == PARENT_PAGE:
            return {"title": {"title": [{"text": {"content": title}}]}}

        properties = {self.title_prop_name: {"title": [{"text": {"content": title}}]}}
        if self.date_prop_name:
            properties[self.date_prop_name] = {
                "date": {"start": datetime.now().replace(microsecond=0).isoformat()}
            }
        return properties

    def create_report(
        self,
        snapshot,
        news_data,
        ai_comment=None,
        universe_review=None,
        bucket_allocation=None,
    ):
        """Create a page under the configured database or page.

        Returns {page_id, url, title}.
        """
        title = f"{self.title_prefix} - {datetime.now().strftime('%Y-%m-%d %H:%M')}"
        children_blocks = []

        # 1. AI Analysis first - the reason to open the page at all. Gemini
        #    replies in markdown (**bold**, numbered sections, bullets); Notion's
        #    API takes rich-text spans instead, so the reply is parsed into
        #    proper blocks rather than dumped into one block as a literal string
        #    (which would show the raw "**" and "*" markers instead of styling).
        if ai_comment:
            children_blocks.append(self._create_heading_block("🧠 AI Analyst Insight"))
            children_blocks.extend(_markdown_to_blocks(ai_comment))

        # 2. Summary
        children_blocks.append(self._create_heading_block("📊 Asset Summary"))
        children_blocks.extend(self._summary_blocks(snapshot))
        children_blocks.extend(self._bucket_blocks(bucket_allocation))

        # 3. Holdings
        if snapshot.positions:
            children_blocks.append(self._create_subheading_block("Held Stocks"))
            for position in snapshot.positions:
                children_blocks.append(
                    self._create_bullet_block(_position_line(position))
                )

        # 4. Risk warnings surfaced by the API - facts, so they outrank the AI.
        if snapshot.warnings:
            children_blocks.append(self._create_heading_block("⚠️ Risk Warnings"))
            for warning in snapshot.warnings:
                children_blocks.append(self._create_bullet_block(warning))

        # 5. AI universe review. Placed above the news it was derived from,
        #    and split in two on purpose: one list already changed what the
        #    engine will do, the other is a suggestion nobody has acted on.
        #    A reader must never have to work out which is which.
        if universe_review is not None and not universe_review.is_empty:
            children_blocks.append(self._create_heading_block("🤖 AI Universe Review"))
            if universe_review.vetoes:
                children_blocks.append(
                    self._create_subheading_block("신규 매수 보류 (자동 적용됨)")
                )
                for veto in universe_review.vetoes:
                    children_blocks.append(
                        self._create_bullet_block(
                            f"{veto.symbol} [{veto.category}] — {veto.reason} "
                            f"(근거: {veto.evidence})"
                        )
                    )
            if universe_review.candidates:
                children_blocks.append(
                    self._create_subheading_block("편입 후보 제안 (검토용 · 자동 반영 안 됨)")
                )
                for candidate in universe_review.candidates:
                    children_blocks.append(
                        self._create_bullet_block(
                            f"{candidate.symbol} ({candidate.name}) — {candidate.reason}"
                        )
                    )

        # 6. News
        children_blocks.append(self._create_heading_block("📰 Economic News"))
        children_blocks.append(self._create_subheading_block("General Economy"))
        for item in news_data.get("general", []):
            children_blocks.append(self._create_bullet_block(item["title"], item["link"]))

        for keyword, items in news_data.get("keywords", {}).items():
            if items:
                children_blocks.append(self._create_subheading_block(f"News: {keyword}"))
                for item in items:
                    children_blocks.append(
                        self._create_bullet_block(item["title"], item["link"])
                    )

        # Notion takes at most 100 blocks per request: create the page with
        # the first batch and append the rest in order.
        page = self.client.pages.create(
            parent=self._parent(),
            properties=self._properties(title),
            children=children_blocks[:_MAX_BLOCKS_PER_REQUEST],
        )
        for start in range(_MAX_BLOCKS_PER_REQUEST, len(children_blocks), _MAX_BLOCKS_PER_REQUEST):
            self.client.blocks.children.append(
                block_id=page.get("id"),
                children=children_blocks[start:start + _MAX_BLOCKS_PER_REQUEST],
            )
        print(f"[Notion] Successfully created report: {title}")
        return {"page_id": page.get("id"), "url": page.get("url"), "title": title}

    def _bucket_blocks(self, allocation):
        """Where the portfolio actually sits against the plan it is meant to hold.

        A strategy organised around bucket weights whose report never shows
        them can drift to half its safe weight for months while every screen
        looks normal - the same silence as a job that stopped running. The
        gap is printed next to each share so "off plan" is readable without
        arithmetic.
        """
        if not allocation:
            return []

        order = {BUCKET_SAFE: 0, BUCKET_CORE: 1, BUCKET_GROWTH: 2, UNMANAGED: 3}
        lines = []
        for bucket in sorted(allocation, key=lambda b: order.get(b, 9)):
            row = allocation[bucket]
            share = row.get("share") or ZERO
            target = row.get("target")
            label = _BUCKET_LABELS.get(bucket, bucket)
            if target is None:
                lines.append(f"{label}: {share:.1%} (전체 자산 대비 · 계획 없음)")
                continue
            gap = share - target
            arrow = "▲" if gap > 0 else ("▼" if gap < 0 else "=")
            lines.append(
                f"{label}: {share:.1%} / 목표 {target:.0%} {arrow} {abs(gap):.1%}"
            )
        blocks = [
            self._create_subheading_block("Allocation vs Plan"),
            self._create_paragraph_block("\n".join(lines)),
        ]

        unmanaged = allocation.get(UNMANAGED)
        if unmanaged and (unmanaged.get("share") or ZERO) > ZERO:
            symbols = ", ".join(unmanaged.get("symbols") or [])
            blocks.append(
                self._create_callout_block(
                    f"전략 외 보유 {unmanaged['share']:.1%}"
                    f"{f' ({symbols})' if symbols else ''} — 따로 관리하는 몫이라 "
                    "위 버킷 비중 계산에서 제외했습니다. 전략은 이 종목들을 "
                    "사지도 팔지도 않습니다.",
                    emoji="📌",
                )
            )
        return blocks

    def _summary_blocks(self, snapshot):
        blocks = []
        lines = [
            f"💰 Total Assets: {_fmt_krw(snapshot.total_krw)}",
            f"📥 Invested: {_fmt_krw(snapshot.purchase_krw)}",
            f"📈 Total P&L: {_fmt_signed(snapshot.profit_krw)} "
            f"({_fmt_pct(snapshot.profit_rate)})",
        ]
        if snapshot.profit_rate_after_cost is not None:
            lines.append(
                f"🧾 After fees & tax: {_fmt_signed(snapshot.profit_after_cost_krw)} "
                f"({_fmt_pct(snapshot.profit_rate_after_cost)})"
            )
        if snapshot.daily_profit_krw:
            lines.append(
                f"📅 Today's P&L: {_fmt_signed(snapshot.daily_profit_krw)} "
                f"({_fmt_pct(snapshot.daily_profit_rate)})"
            )
        lines.append(f"💱 USD/KRW: {Decimal(snapshot.exchange_rate):,.2f}")
        blocks.append(self._create_paragraph_block("\n".join(lines)))

        if snapshot.has_unconverted_fx:
            # Without a purchase-time rate the return excludes FX gain/loss.
            # Saying so beats letting the number be read as something else.
            blocks.append(
                self._create_callout_block(
                    "환차손익 미반영 — 일부 해외 종목에 매수 시점 환율"
                    "(avg_exchange_rate)이 없어 해당 종목의 수익률은 주가 손익만 "
                    "반영합니다.",
                    emoji="⚠️",
                )
            )
        return blocks

    def _create_heading_block(self, text):
        return {
            "object": "block",
            "type": "heading_2",
            "heading_2": {"rich_text": [{"type": "text", "text": {"content": text}}]},
        }

    def _create_subheading_block(self, text):
        return {
            "object": "block",
            "type": "heading_3",
            "heading_3": {"rich_text": [{"type": "text", "text": {"content": text}}]},
        }

    def _create_paragraph_block(self, text):
        return {
            "object": "block",
            "type": "paragraph",
            "paragraph": {"rich_text": [{"type": "text", "text": {"content": text}}]},
        }

    def _create_callout_block(self, text, emoji="🧠"):
        return {
            "object": "block",
            "type": "callout",
            "callout": {
                "rich_text": [{"type": "text", "text": {"content": text}}],
                "icon": {"type": "emoji", "emoji": emoji},
            },
        }

    def _create_bullet_block(self, text, url=None):
        rich_text = [{"type": "text", "text": {"content": text}}]
        if url:
            rich_text[0]["text"]["link"] = {"url": url}

        return {
            "object": "block",
            "type": "bulleted_list_item",
            "bulleted_list_item": {"rich_text": rich_text},
        }


_INLINE_MARKDOWN = re.compile(r"\*\*(.+?)\*\*|\*(.+?)\*")


def _inline_rich_text(text):
    """Turn ``**bold**`` / ``*italic*`` markdown spans into Notion rich_text.

    Notion's API renders rich_text content literally - it does not parse
    markdown the way typing into the editor does - so the AI's markdown
    reply needs its emphasis markers converted into annotation objects.
    """
    spans = []
    pos = 0
    for match in _INLINE_MARKDOWN.finditer(text):
        if match.start() > pos:
            spans.append((text[pos:match.start()], {}))
        if match.group(1) is not None:
            spans.append((match.group(1), {"bold": True}))
        else:
            spans.append((match.group(2), {"italic": True}))
        pos = match.end()
    if pos < len(text):
        spans.append((text[pos:], {}))

    rich_text = []
    for content, annotations in spans:
        if not content:
            continue
        # Notion rejects a text object whose content exceeds 2000 characters.
        for chunk_start in range(0, len(content), 2000):
            chunk = content[chunk_start:chunk_start + 2000]
            obj = {"type": "text", "text": {"content": chunk}}
            if annotations:
                obj["annotations"] = annotations
            rich_text.append(obj)
    if not rich_text:
        rich_text = [{"type": "text", "text": {"content": ""}}]
    return rich_text


_NUMBERED_HEADING = re.compile(r"^(?:\*\*)?(\d+\.\s+.*?)(?:\*\*)?$")
_BULLET_LINE = re.compile(r"^[*\-•]\s+(.*)")


def _markdown_to_blocks(text):
    """Parse a markdown reply into Notion blocks (headings/paragraphs/bullets).

    Handles the shape the analyst prompt asks for: numbered section titles,
    ``*``/``-`` bullets (one level of indentation nested as block children),
    and inline ``**bold**``/``*italic*`` spans. Anything else falls back to
    a plain paragraph so unexpected formatting still renders as text.
    """
    blocks = []
    last_top_bullet = None
    for raw_line in text.strip().splitlines():
        if not raw_line.strip():
            last_top_bullet = None
            continue

        indent = len(raw_line) - len(raw_line.lstrip(" "))
        stripped = raw_line.strip()

        bullet_match = _BULLET_LINE.match(stripped)
        heading_match = _NUMBERED_HEADING.match(stripped)

        if bullet_match:
            block = {
                "object": "block",
                "type": "bulleted_list_item",
                "bulleted_list_item": {"rich_text": _inline_rich_text(bullet_match.group(1))},
            }
            if indent >= 2 and last_top_bullet is not None:
                last_top_bullet["bulleted_list_item"].setdefault("children", []).append(block)
            else:
                blocks.append(block)
                last_top_bullet = block
        elif heading_match:
            blocks.append(
                {
                    "object": "block",
                    "type": "heading_3",
                    "heading_3": {"rich_text": _inline_rich_text(heading_match.group(1))},
                }
            )
            last_top_bullet = None
        else:
            blocks.append(
                {
                    "object": "block",
                    "type": "paragraph",
                    "paragraph": {"rich_text": _inline_rich_text(stripped)},
                }
            )
            last_top_bullet = None
    return blocks


def _position_line(position):
    label = position.name or position.symbol or "?"
    badge = "토스" if position.source == "toss" else "수기"
    parts = [f"[{badge}] {label}: {position.quantity} @ {position.last_price:,} {position.currency}"]
    if position.profit_rate is not None:
        parts.append(_fmt_pct(position.profit_rate))
    return " · ".join(parts)


def _get(source, key, default=None):
    if isinstance(source, dict):
        return source.get(key, default)
    return getattr(source, key, default)
