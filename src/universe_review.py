"""AI review of the trading universe: what to drop, what to consider adding.

Two powers, deliberately unequal.

**Veto** (applied automatically). The model may block *new buys* of a symbol
already in the universe. It may never sell, never buy, and never raise more
than ``max_vetoes`` of them in one run. A veto is only accepted for a
structural fact about the security - a delisting, a merger, a ticker change, a
trading halt, an accounting investigation - never for an opinion about price,
valuation or prospects. Ranking by prospects is the strategy's job, and the
strategy can be backtested; this cannot.

**Proposal** (never applied). The model may suggest symbols *outside* the
universe worth a human look. These are written to the report and to storage
and go no further. Adding a name to the universe stays a human edit of
config.yaml.

Proposals come in two lists: ordinary large caps that widen the universe, and
*growth* candidates - young companies in industries that may matter later,
for the GROWTH bucket. Both stop at the report. Symbols the user has taken out
on purpose (``excluded_candidates``) are never proposed back.

The asymmetry is the whole design. A wrong veto costs one missed opportunity
in one name for a few days. A wrong buy costs money, and a universe the model
quietly rewrites is a strategy no backtest describes - which is exactly the
failure the 2026-08-27 backtest was run to avoid. So the power that can only
subtract is automatic, and the power that adds is advisory.

Every veto must cite evidence from the headlines it was given. One with an
empty citation is dropped before it reaches storage: a model that cannot say
why is a model that is guessing.
"""

import json
from dataclasses import dataclass

from google.genai import types

from src.analyst import ai_settings, gemini_client
from src.strategy.universe import BUCKET_CORE, BUCKET_GROWTH

#: Reasons a veto is allowed to exist. Sent to the model, and the returned
#: category is checked against this list - a veto filed under anything else
#: (or under nothing) is dropped. Free-text reasons alone would let
#: "outlook deteriorating" through as if it were a delisting notice.
VETO_CATEGORIES = (
    "delisting",  # 상장폐지 절차 개시·확정
    "merger_or_acquisition",  # 피인수·합병으로 종목 소멸 예정
    "ticker_change",  # 티커/심볼 변경
    "trading_halt",  # 거래정지
    "accounting_or_fraud_investigation",  # 회계 부정·규제 조사
    "bankruptcy_or_restructuring",  # 파산·법정관리
)

_MAX_REASON_CHARS = 300

_CANDIDATE_LIST_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "symbol": {"type": "string"},
            "name": {"type": "string"},
            "reason": {"type": "string"},
        },
        "required": ["symbol", "name", "reason"],
    },
}

_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "vetoes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string"},
                    "category": {"type": "string", "enum": list(VETO_CATEGORIES)},
                    "reason": {"type": "string"},
                    "evidence": {"type": "string"},
                },
                "required": ["symbol", "category", "reason", "evidence"],
            },
        },
        "candidates": _CANDIDATE_LIST_SCHEMA,
        "growth_candidates": _CANDIDATE_LIST_SCHEMA,
    },
    "required": ["vetoes", "candidates", "growth_candidates"],
}


@dataclass(frozen=True)
class Veto:
    """A block on *new buys* of one universe member. Never a sell."""

    symbol: str
    category: str
    reason: str
    evidence: str


@dataclass(frozen=True)
class Candidate:
    """A symbol the model suggests a human consider adding. Advisory only."""

    symbol: str
    name: str
    reason: str
    #: The bucket it was proposed for. ``BUCKET_GROWTH`` for a growth
    #: candidate, ``BUCKET_CORE`` otherwise.
    bucket: str = BUCKET_CORE


@dataclass(frozen=True)
class UniverseReview:
    vetoes: tuple = ()
    candidates: tuple = ()
    growth_candidates: tuple = ()
    #: Set when the review could not be produced. The caller reports it; it
    #: never becomes an empty-but-successful review, because "the model said
    #: nothing is wrong" and "we could not ask" are different facts.
    error: str | None = None

    @property
    def is_empty(self):
        return not self.vetoes and not self.candidates and not self.growth_candidates


def _clip(text, limit=_MAX_REASON_CHARS):
    text = " ".join(str(text or "").split())
    return text[:limit]


def _headlines(news_data, per_section=3):
    lines = []
    for item in (news_data or {}).get("general", [])[:5]:
        lines.append(f"- (general) {item.get('title')}")
    for keyword, items in ((news_data or {}).get("keywords") or {}).items():
        for item in (items or [])[:per_section]:
            lines.append(f"- ({keyword}) {item.get('title')}")
    return "\n".join(lines) or "None"


class UniverseReviewer:
    """One Gemini call per daily report, returning a sanitised review."""

    def __init__(self, config, client=None):
        api_key, self.model_name, self.thinking_level = ai_settings(config)
        analyst_cfg = getattr(config, "analyst", None)
        self.enabled = getattr(analyst_cfg, "universe_review", True)
        self.max_vetoes = getattr(analyst_cfg, "max_vetoes", 3)
        self.max_candidates = getattr(analyst_cfg, "max_candidates", 5)
        self.max_growth_candidates = getattr(analyst_cfg, "max_growth_candidates", 3)
        self.excluded = {
            str(s).strip().upper()
            for s in getattr(analyst_cfg, "excluded_candidates", ()) or ()
        }
        self.client = client or gemini_client(api_key)

    def review(self, universe_symbols, holdings=(), news_data=None, growth_symbols=()):
        """Review ``universe_symbols`` against today's headlines.

        Returns a :class:`UniverseReview` whose contents have already been
        checked against the universe - callers never have to re-validate what
        the model said.
        """
        if not self.enabled:
            return UniverseReview(error="universe_review가 꺼져 있습니다.")
        if not self.client:
            return UniverseReview(error="AI 미설정 (API 키 없음)")

        known = {str(s).upper() for s in universe_symbols}
        if not known:
            return UniverseReview(error="유니버스가 비어 있습니다.")

        try:
            raw = self._ask(sorted(known), holdings, news_data, growth_symbols)
        except Exception as exc:  # noqa: BLE001 - a daily batch must not die here
            print(f"Error reviewing universe: {exc}")
            return UniverseReview(error=f"AI 유니버스 검토 실패: {exc}")

        # Growth first, so a name the model put in both lists is shown where
        # it was specifically argued for.
        growth = self._clean_candidates(
            raw.get("growth_candidates"),
            known,
            self.max_growth_candidates,
            BUCKET_GROWTH,
        )
        taken = known | {c.symbol for c in growth}
        return UniverseReview(
            vetoes=self._clean_vetoes(raw.get("vetoes"), known),
            candidates=self._clean_candidates(
                raw.get("candidates"), taken, self.max_candidates, BUCKET_CORE
            ),
            growth_candidates=growth,
        )

    # ------------------------------------------------------------- internals

    def _ask(self, symbols, holdings, news_data, growth_symbols=()):
        held = ", ".join(str(s) for s in holdings) or "None"
        growth = ", ".join(str(s) for s in growth_symbols) or "None"
        excluded = ", ".join(sorted(self.excluded)) or "None"
        prompt = f"""
You are reviewing the *tradable universe* of an automated momentum strategy.
You are NOT picking stocks and NOT forecasting returns - a backtested ranking
rule already does that, and it does it better than prose can.

Current universe ({len(symbols)} symbols):
{", ".join(symbols)}

Currently held: {held}

Of these, the GROWTH bucket (young companies in future industries): {growth}

Never propose these - the user removed them on purpose: {excluded}

Today's headlines (the ONLY evidence you may cite):
{_headlines(news_data)}

Produce two lists.

1. "vetoes" - universe members whose NEW PURCHASES should be paused because of
   a structural fact about the security itself. Allowed categories only:
   {", ".join(VETO_CATEGORIES)}.
   A veto is NOT for: a weak quarter, a high valuation, a downgrade, a falling
   price, a sector you dislike, or any forecast. If today's headlines give you
   no such structural fact, return an EMPTY list - that is the normal answer
   on almost every day, and an empty list is a better answer than a stretched
   one. Every veto must quote the specific headline it rests on in
   "evidence". No headline, no veto. At most {self.max_vetoes}.

2. "candidates" - up to {self.max_candidates} liquid, large-cap US-listed
   symbols NOT already in the universe that a human might reasonably consider
   adding, with a one-sentence reason. These are suggestions for a person to
   review; nothing is bought from this list. Prefer names that widen sector
   coverage over more of what the universe already holds. No leveraged,
   inverse, single-stock or otherwise exotic ETFs. Return an empty list if
   nothing stands out.

3. "growth_candidates" - up to {self.max_growth_candidates} US-listed common
   stocks NOT already in the universe for the GROWTH bucket: younger companies
   in industries that may matter over the next decade (e.g. space, quantum
   computing, AI infrastructure, robotics, advanced nuclear, gene editing).
   They must trade on NYSE or Nasdaq with meaningful daily volume - no OTC,
   no SPAC shells, no pre-revenue penny stocks, no ETFs of any kind. Prefer
   an industry the GROWTH bucket does not cover yet. In "reason", say in one
   or two sentences why it fits and what its main risk is. Return an empty
   list if nothing stands out.

Write "reason" in Korean. Symbols must be plain tickers, uppercase.
""".strip()

        response = self.client.models.generate_content(
            model=self.model_name,
            contents=prompt,
            config=self._config(),
        )
        text = (response.text or "").strip()
        if not text:
            raise ValueError("모델이 빈 응답을 반환했습니다.")
        return json.loads(text)

    def _config(self):
        level = (self.thinking_level or "").strip().lower()
        thinking = (
            types.ThinkingConfig(thinking_level=level)
            if level and level != "off"
            else None
        )
        return types.GenerateContentConfig(
            thinking_config=thinking,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(
                disable=True
            ),
            response_mime_type="application/json",
            response_json_schema=_RESPONSE_SCHEMA,
        )

    def _clean_vetoes(self, rows, known):
        """Keep only vetoes that name a universe member, a permitted category
        and a piece of evidence - then cap the count."""
        cleaned = []
        seen = set()
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            symbol = str(row.get("symbol") or "").strip().upper()
            category = str(row.get("category") or "").strip()
            evidence = _clip(row.get("evidence"))
            if symbol not in known or symbol in seen:
                continue
            if category not in VETO_CATEGORIES or not evidence:
                continue
            seen.add(symbol)
            cleaned.append(
                Veto(
                    symbol=symbol,
                    category=category,
                    reason=_clip(row.get("reason")) or category,
                    evidence=evidence,
                )
            )
            if len(cleaned) >= self.max_vetoes:
                break
        return tuple(cleaned)

    def _clean_candidates(self, rows, known, limit, bucket):
        cleaned = []
        seen = set()
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            symbol = str(row.get("symbol") or "").strip().upper()
            # A "candidate" already in the universe is noise, not a suggestion.
            if not symbol or symbol in known or symbol in seen:
                continue
            # The model may not know, or may not care, that the user took a
            # name out. Proposing it back every morning is noise.
            if symbol in self.excluded:
                continue
            seen.add(symbol)
            cleaned.append(
                Candidate(
                    symbol=symbol,
                    name=_clip(row.get("name"), 80) or symbol,
                    reason=_clip(row.get("reason")),
                    bucket=bucket,
                )
            )
            if len(cleaned) >= limit:
                break
        return tuple(cleaned)
