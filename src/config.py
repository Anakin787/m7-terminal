"""Configuration loading for M7 Terminal.

Credentials come from the environment first and ``config.yaml`` only as a
fallback. From Phase 2 the same Toss credential carries order-placement
rights, so keeping it out of a file that sits next to the code matters more
than the convenience of having everything in one place.
"""

import os
import warnings
from dataclasses import dataclass, field
from decimal import Decimal

import yaml

try:
    from dotenv import load_dotenv
except ImportError:  # dotenv is optional; env vars still work without it
    load_dotenv = None

from src.toss.errors import TossConfigError

#: Overridable because the file does not always sit next to the code. On Cloud
#: Run it arrives as a mounted secret, and it cannot be mounted at
#: ``/app/config.yaml`` - a volume mount there would shadow the application
#: directory it lands in. So the deployment says where it put it.
DEFAULT_CONFIG_PATH = os.environ.get("M7_CONFIG_PATH") or "config.yaml"
DEFAULT_TOKEN_CACHE = ".toss_token.json"
DEFAULT_BASE_URL = "https://openapi.tossinvest.com"

#: Notion token placeholder shipped in the README/example config.
NOTION_TOKEN_PLACEHOLDER = "secret_YOUR_NOTION_TOKEN_HERE"

#: yfinance suffixes that Toss does not use. Toss identifies KR stocks by the
#: bare 6-digit code, so 005930.KS has to become 005930.
_YF_SUFFIXES = (".KS", ".KQ")

#: Placeholder prefix used by .env.example. Treated as "not set" so a
#: half-filled .env reports which key is missing instead of failing later
#: with a 401 that looks like a wrong credential.
_PLACEHOLDER_PREFIX = "PASTE_"


def is_placeholder(value):
    return bool(value) and str(value).startswith(_PLACEHOLDER_PREFIX)


def _as_bool(value, default):
    """Read a YAML-ish truth value, falling back rather than raising.

    An unreadable optional flag must not stop the daily report; the default
    is the conservative reading in every case it is used.
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("true", "yes", "on", "1"):
        return True
    if text in ("false", "no", "off", "0"):
        return False
    return default


def _as_int(value, default):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0 else default


def normalize_symbol(symbol):
    """Convert a yfinance-style ticker to Toss notation.

    005930.KS -> 005930; US tickers such as AAPL are unchanged. Lets an
    existing v1 config be read without hand-editing every entry.
    """
    if not symbol:
        return symbol
    text = str(symbol).strip()
    upper = text.upper()
    for suffix in _YF_SUFFIXES:
        if upper.endswith(suffix):
            return text[: -len(suffix)]
    return text


def mask_secret(value):
    """Render a secret as abcd...wxyz so logs stay useful but harmless."""
    if not value:
        return ""
    text = str(value)
    if len(text) <= 8:
        return "*" * len(text)
    return f"{text[:4]}...{text[-4:]}"


@dataclass(frozen=True)
class ManualHolding:
    """A position held outside the Toss account, entered by hand.

    Only qty and avg_price are genuinely manual - the name, currency and
    current price are filled in from the Toss market-data endpoints. A
    holding with no symbol (cash, gold, ...) must supply price.
    """

    symbol: str | None
    qty: Decimal
    avg_price: Decimal
    name: str | None = None
    currency: str | None = None
    price: Decimal | None = None
    avg_exchange_rate: Decimal | None = None
    #: The single company this product actually tracks, for a leveraged or
    #: single-stock ETF (TSLL -> "TSLA", IONX -> "IONQ"). Declared here, not
    #: guessed from the product's name - a search for the full listed name
    #: ("DIREXION DAILY TSLA BULL 2X SHARES") returns almost nothing useful
    #: on a general news search, where the underlying company's own name
    #: does. None for anything that isn't tracking a single other security.
    underlying: str | None = None

    @property
    def is_static(self):
        """True when there is no symbol to look up a live price for."""
        return not self.symbol


@dataclass(frozen=True)
class SavingsPlan:
    """A recurring safe-asset contribution outside the brokerage account.

    Not a position - no ticker, no market value, nothing for the portfolio
    aggregator to price. It exists purely as a fact handed to the AI analyst
    (src/analyst.py) so its risk and allocation commentary accounts for safe
    assets the user already builds elsewhere (a 적금, a CMA, a pension
    contribution, ...) instead of judging the brokerage account in isolation.
    """

    name: str
    monthly_krw: Decimal
    kind: str = "적금"


@dataclass(frozen=True)
class TossConfig:
    client_id: str
    client_secret: str
    account_no: str | None = None
    base_url: str = DEFAULT_BASE_URL
    token_cache: str = DEFAULT_TOKEN_CACHE

    def __repr__(self):  # never let the secret reach a log or traceback
        return (
            f"TossConfig(client_id={mask_secret(self.client_id)!r}, "
            f"client_secret={mask_secret(self.client_secret)!r}, "
            f"account_no={self.account_no!r}, base_url={self.base_url!r})"
        )


@dataclass(frozen=True)
class NotionConfig:
    token: str
    database_id: str
    page_title_prefix: str = "Financial Report"

    @property
    def is_configured(self):
        return (
            bool(self.token)
            and self.token != NOTION_TOKEN_PLACEHOLDER
            and bool(self.database_id)
            and self.database_id != "YOUR_DATABASE_ID"
        )


#: Google retired the gemini-1.5 line; 3.7 Flash is the current default model.
DEFAULT_AI_MODEL = "gemini-3.7-flash"

#: Reasoning depth for the daily commentary. See src/analyst.py for why "low".
DEFAULT_THINKING_LEVEL = "low"


@dataclass(frozen=True)
class AnalystConfig:
    api_key: str | None = None
    model: str = DEFAULT_AI_MODEL
    thinking_level: str = DEFAULT_THINKING_LEVEL

    #: Ask the model, once per daily report, which universe members look
    #: structurally broken (a veto) and which non-members are worth a human
    #: look (a proposal). See src/universe_review.py for why those two powers
    #: are not symmetric.
    universe_review: bool = True
    #: Hard ceiling on vetoes per run. A model that decides everything is
    #: broken must not be able to halt the whole strategy.
    max_vetoes: int = 3
    max_candidates: int = 5
    #: A veto lapses this many days after the run that raised it, unless a
    #: later run raises it again. Nothing an AI says stays true by default.
    veto_ttl_days: int = 7


DEFAULT_KILL_SWITCH_PATH = "KILL_SWITCH"


@dataclass(frozen=True)
class TradingConfig:
    """Phase 2 settings. Off unless explicitly enabled.

    ``enabled`` defaults to False so that adding the trading code to the tree
    does not, by itself, make the daily job start placing orders. Turning it
    on is a separate, deliberate edit.
    """

    enabled: bool = False
    kill_switch_path: str = DEFAULT_KILL_SWITCH_PATH
    #: ``module:Class`` paths, imported at run time. Strategies live outside
    #: this repo's core - the engine does not ship an opinion about what to
    #: trade.
    strategies: list = field(default_factory=list)
    limits: dict = field(default_factory=dict)
    #: Raw instrument rows for ``src.strategy.universe.parse_universe``. Kept
    #: raw (not parsed into a Universe here) so this module does not need to
    #: import the strategy layer - the same separation ``risk_limits()``
    #: already keeps for RiskLimits.
    universe: list = field(default_factory=list)
    #: Free-form parameters for whichever strategy is loaded. Config does not
    #: validate the keys - a strategy's parameters are that strategy's
    #: business, the same way its module path is.
    strategy_params: dict = field(default_factory=dict)

    #: ``{symbol: quantity}`` the engine must act as if it does not hold.
    #:
    #: Shares bought by hand, for reasons the strategy knows nothing about.
    #: Without this the engine reads them as its own: it counts them toward
    #: its target and stops buying, and - the part that costs money - a
    #: rotation exit sells the whole position, because ``exit_fraction`` is a
    #: fraction of what the account holds, not of what the strategy bought.
    #: Neither the AI veto nor the risk gate would stop that; a veto blocks
    #: buys only, by design.
    #:
    #: Distinct from ``portfolio.manual``, which is holdings at *other*
    #: brokers and exists for reporting. These are real Toss holdings, fully
    #: reported, that the trading engine alone is blind to.
    excluded_holdings: dict = field(default_factory=dict)

    #: How far out an OCO bracket's expireDate is set, once an entry fills.
    #: Toss requires an expiry (design 2.3); this is not a trading decision,
    #: just how long the exit order is allowed to keep watching.
    oco_expire_days: int = 30
    #: The stop leg's order price sits this fraction below its trigger, so a
    #: fast-moving break does not pass the limit before it reaches the book.
    oco_stop_loss_slippage: Decimal = Decimal("0.005")

    def risk_limits(self):
        """Build a RiskLimits from the config, keeping every default."""
        from src.execution.risk import RiskLimits

        return RiskLimits(**self.limits) if self.limits else RiskLimits()


@dataclass(frozen=True)
class AppConfig:
    toss: TossConfig
    notion: NotionConfig
    analyst: AnalystConfig
    manual_holdings: list = field(default_factory=list)
    savings_plans: list = field(default_factory=list)
    news_keywords: list = field(default_factory=list)
    trading: TradingConfig = field(default_factory=TradingConfig)


def _decimal(value, field_name):
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception as exc:
        raise TossConfigError(
            f"'{field_name}' 값을 숫자로 읽을 수 없습니다: {value!r}"
        ) from exc


def _load_yaml(path):
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def _resolve_credentials(raw_toss):
    """Environment first, config file second."""
    env_secret = os.environ.get("TOSS_CLIENT_SECRET")
    client_id = os.environ.get("TOSS_CLIENT_ID") or raw_toss.get("client_id")
    client_secret = env_secret or raw_toss.get("client_secret")

    if not env_secret and raw_toss.get("client_secret"):
        warnings.warn(
            "client_secret을 config.yaml에서 읽었습니다. Phase 2부터 이 자격증명은 "
            "주문 실행 권한을 가지므로 .env의 TOSS_CLIENT_SECRET으로 옮기는 것을 권장합니다.",
            stacklevel=2,
        )

    pairs = (("TOSS_CLIENT_ID", client_id), ("TOSS_CLIENT_SECRET", client_secret))
    missing = [name for name, value in pairs if not value]
    unfilled = [name for name, value in pairs if is_placeholder(value)]

    if unfilled:
        raise TossConfigError(
            f".env의 {', '.join(unfilled)} 가 아직 플레이스홀더입니다. "
            "토스증권 WTS > 설정 > Open API 에서 발급받은 값으로 교체하세요."
        )
    if missing:
        raise TossConfigError(
            f"토스 자격증명이 없습니다: {', '.join(missing)}. "
            ".env 파일에 설정하거나 환경변수로 전달하세요."
        )
    return client_id, client_secret


def _parse_manual_holdings(raw_portfolio):
    """Read portfolio.manual, falling back to the v1 portfolio.stocks."""
    entries = raw_portfolio.get("manual")
    if entries is None:
        entries = raw_portfolio.get("stocks")
        if entries:
            warnings.warn(
                "config.yaml의 'portfolio.stocks'는 v1 스키마입니다. "
                "'portfolio.manual'로 이름을 바꿔주세요 (이번에는 자동 변환했습니다).",
                stacklevel=2,
            )
    entries = entries or []

    holdings = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise TossConfigError(f"portfolio 항목 {index}가 매핑이 아닙니다: {entry!r}")

        symbol = normalize_symbol(entry.get("symbol"))
        qty = _decimal(entry.get("qty"), f"portfolio[{index}].qty")
        avg_price = _decimal(entry.get("avg_price"), f"portfolio[{index}].avg_price")
        price = _decimal(entry.get("price"), f"portfolio[{index}].price")

        if qty is None:
            raise TossConfigError(f"portfolio[{index}]에 'qty'가 없습니다.")
        if avg_price is None:
            raise TossConfigError(f"portfolio[{index}]에 'avg_price'가 없습니다.")
        if not symbol and price is None:
            raise TossConfigError(
                f"portfolio[{index}]에 'symbol'이 없으면 'price'를 직접 입력해야 합니다."
            )

        holdings.append(
            ManualHolding(
                symbol=symbol,
                qty=qty,
                avg_price=avg_price,
                name=entry.get("name"),
                currency=entry.get("currency"),
                price=price,
                avg_exchange_rate=_decimal(
                    entry.get("avg_exchange_rate"),
                    f"portfolio[{index}].avg_exchange_rate",
                ),
                underlying=entry.get("underlying"),
            )
        )
    return holdings


def _parse_savings_plans(raw_portfolio):
    """Read portfolio.savings - recurring safe-asset contributions.

    Unlike manual holdings these are not looked up anywhere; they carry no
    ticker to price. A malformed entry fails at load time rather than
    quietly not showing up in the AI's context, the same convention
    ``_parse_manual_holdings`` follows.
    """
    entries = raw_portfolio.get("savings") or []
    plans = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise TossConfigError(f"portfolio.savings[{index}]가 매핑이 아닙니다: {entry!r}")

        name = entry.get("name")
        if not name:
            raise TossConfigError(f"portfolio.savings[{index}]에 'name'이 없습니다.")

        monthly_krw = _decimal(
            entry.get("monthly_krw"), f"portfolio.savings[{index}].monthly_krw"
        )
        if monthly_krw is None:
            raise TossConfigError(f"portfolio.savings[{index}]에 'monthly_krw'가 없습니다.")

        plans.append(
            SavingsPlan(name=name, monthly_krw=monthly_krw, kind=entry.get("kind") or "적금")
        )
    return plans


#: Limit names accepted under ``trading.limits``, and how to read each one.
#: Money and ratios go through _decimal rather than float(): a limit read as
#: a float would put rounding drift back into a pipeline built on Decimal.
_RISK_LIMIT_FIELDS = {
    "max_orders_per_day": int,
    "max_daily_notional_krw": "decimal",
    "max_position_weight": "decimal",
    "high_value_threshold_krw": "decimal",
    "allow_high_value": bool,
    "amount_order_cutoff_minutes": int,
    "strict": bool,
    #: Per-symbol exception to max_position_weight. A dict, not a scalar - see
    #: the dedicated branch below rather than the generic loop.
    "max_position_weight_overrides": "weight_map",
    #: Below this equity, the weight check is skipped entirely. A strategy's
    #: very first order is, by definition, 100% of the account; without this,
    #: no concentrated strategy could ever place one.
    "weight_check_min_equity_krw": "decimal",
}


def _parse_weight_overrides(value):
    if not isinstance(value, dict):
        raise TossConfigError(
            "'trading.limits.max_position_weight_overrides'는 "
            f"{{심볼: 비중}} 매핑이어야 합니다: {value!r}"
        )
    parsed = {}
    for symbol, weight in value.items():
        parsed[str(symbol)] = _decimal(
            weight, f"trading.limits.max_position_weight_overrides.{symbol}"
        )
    return parsed


def _parse_trading(raw_trading):
    raw_trading = raw_trading or {}
    raw_limits = raw_trading.get("limits") or {}

    unknown = set(raw_limits) - set(_RISK_LIMIT_FIELDS)
    if unknown:
        # A silently ignored limit reads as a limit that is in force.
        raise TossConfigError(
            f"trading.limits에 알 수 없는 항목이 있습니다: {sorted(unknown)}. "
            f"사용 가능: {sorted(_RISK_LIMIT_FIELDS)}"
        )

    limits = {}
    for name, kind in _RISK_LIMIT_FIELDS.items():
        if name not in raw_limits:
            continue
        value = raw_limits[name]
        if kind == "decimal":
            limits[name] = _decimal(value, f"trading.limits.{name}")
        elif kind == "weight_map":
            limits[name] = _parse_weight_overrides(value)
        elif kind is bool:
            limits[name] = bool(value)
        else:
            try:
                limits[name] = int(value)
            except (TypeError, ValueError):
                raise TossConfigError(
                    f"'trading.limits.{name}' 값을 정수로 읽을 수 없습니다: {value!r}"
                ) from None

    universe_rows = raw_trading.get("universe") or []
    if universe_rows:
        # Validated eagerly, at startup, rather than the first time a
        # strategy is constructed - a bad row should fail loudly before any
        # signal is ever evaluated.
        from src.strategy.universe import parse_universe

        parse_universe(universe_rows)

    oco_expire_days = raw_trading.get("oco_expire_days", 30)
    try:
        oco_expire_days = int(oco_expire_days)
    except (TypeError, ValueError):
        raise TossConfigError(
            f"'trading.oco_expire_days' 값을 정수로 읽을 수 없습니다: {oco_expire_days!r}"
        ) from None

    excluded_holdings = {}
    for symbol, quantity in (raw_trading.get("excluded_holdings") or {}).items():
        symbol = str(symbol).strip().upper()
        if not symbol:
            continue
        # Refused rather than skipped. A typo here does not fail loudly on its
        # own - the engine simply resumes managing shares the operator thinks
        # are protected, and the first sign is a sell that should never have
        # happened.
        value = _decimal(quantity, f"trading.excluded_holdings.{symbol}")
        if value <= 0:
            raise TossConfigError(
                f"'trading.excluded_holdings.{symbol}' 수량은 0보다 커야 합니다: {quantity!r}"
            )
        excluded_holdings[symbol] = value

    return TradingConfig(
        enabled=bool(raw_trading.get("enabled", False)),
        kill_switch_path=raw_trading.get("kill_switch_path")
        or DEFAULT_KILL_SWITCH_PATH,
        strategies=[str(s) for s in (raw_trading.get("strategies") or []) if s],
        limits=limits,
        universe=universe_rows,
        strategy_params=raw_trading.get("strategy_params") or {},
        excluded_holdings=excluded_holdings,
        oco_expire_days=oco_expire_days,
        oco_stop_loss_slippage=_decimal(
            raw_trading.get("oco_stop_loss_slippage", "0.005"), "trading.oco_stop_loss_slippage"
        ),
    )


#: Where the Firebase service account lives when nothing says otherwise.
#: Written with a forward slash and split on use, so the path this builds
#: carries the running platform's separator throughout. Joining the whole
#: string produced ``C:\\repo\\secrets/firebase-...`` on Windows - openable,
#: but a path that matches neither what the user typed nor what any other
#: tool would print, which is a poor thing to put in an error message.
DEFAULT_SERVICE_ACCOUNT = "secrets/firebase-service-account.json"

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _translate_path(path):
    """Yield the same file as WSL and as Windows would spell it.

    This repository is genuinely used from both: interactive work happens
    under WSL (``/mnt/c/...``) while the scheduled daily report runs from
    Windows Task Scheduler (``C:\\...``). A ``.env`` written on one side
    names a path the other cannot open, and the failure surfaces as a
    DefaultCredentialsError deep inside google.auth hours later, in a log
    nobody is watching.
    """
    yield path
    lowered = path.replace("\\", "/")
    if lowered.startswith("/mnt/") and len(lowered) > 6 and lowered[6] == "/":
        drive = lowered[5].upper()
        yield f"{drive}:\\" + lowered[7:].replace("/", "\\")
    elif len(lowered) > 2 and lowered[1] == ":":
        yield "/mnt/" + lowered[0].lower() + lowered[2:]


def resolve_service_account(value=None, root=None):
    """An openable path to the service account JSON, or None.

    Tries, in order: the configured path as written, the same path spelled
    for the other OS, and finally the repository's own ``secrets/`` copy.
    Returns None when none of them exist - the caller leaves the environment
    alone and lets google.auth report the original path, which is the one
    the user actually wrote and can fix.
    """
    root = root or _REPO_ROOT
    candidates = []
    if value:
        candidates.extend(_translate_path(str(value)))
        candidates.append(os.path.join(root, os.path.basename(str(value))))
    candidates.append(os.path.join(root, *DEFAULT_SERVICE_ACCOUNT.split("/")))
    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            return candidate
    return None


def _fix_service_account_env(root=None):
    """Point GOOGLE_APPLICATION_CREDENTIALS at a file that actually opens."""
    configured = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    resolved = resolve_service_account(configured, root=root)
    if resolved and resolved != configured:
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = resolved
    return resolved


class ConfigFileMissing(RuntimeError):
    """A batch entrypoint was asked to run without its config file."""


def require_config_file(path=DEFAULT_CONFIG_PATH):
    """Refuse to start a batch run on a config file that never loaded.

    ``load_config`` is deliberately permissive - environment variables alone
    are a valid configuration and the tests rely on that. But main.py and
    trade.py read the *portfolio* from this file, so a missing one does not
    mean "no settings", it means "no holdings": the run would succeed and
    record a portfolio worth nothing.

    That is not hypothetical. On 2026-09-01 a container that had every
    credential but no config.yaml wrote two snapshots of 0 KRW into the live
    chart - the credentials were in the environment, so the FX rate came back
    fine and nothing looked wrong until the chart fell off a cliff.
    """
    if not os.path.exists(path):
        raise ConfigFileMissing(
            f"설정 파일을 찾을 수 없습니다: {path}\n"
            "       자격증명은 환경변수로 충분하지만 보유 종목은 이 파일에만 있습니다.\n"
            "       이대로 실행하면 '자산 0원'이 정상 결과로 기록됩니다.\n"
            "       컨테이너라면 config.yaml을 /app/config.yaml 로 마운트하세요."
        )
    if not _load_yaml(path):
        raise ConfigFileMissing(
            f"설정 파일이 비어 있습니다: {path}\n"
            "       빈 설정은 보유 종목이 없는 것과 구분되지 않습니다."
        )


def load_config(path=DEFAULT_CONFIG_PATH, load_env=True):
    """Build an AppConfig from .env plus config.yaml."""
    if load_env and load_dotenv is not None:
        load_dotenv()
    _fix_service_account_env()

    raw = _load_yaml(path)
    raw_toss = raw.get("toss") or {}
    raw_notion = raw.get("notion") or {}
    raw_ai = raw.get("google_ai") or {}
    raw_portfolio = raw.get("portfolio") or {}
    raw_news = raw.get("news") or {}
    raw_trading = raw.get("trading") or {}

    client_id, client_secret = _resolve_credentials(raw_toss)

    toss = TossConfig(
        client_id=client_id,
        client_secret=client_secret,
        account_no=raw_toss.get("account_no"),
        base_url=raw_toss.get("base_url", DEFAULT_BASE_URL),
        token_cache=raw_toss.get("token_cache", DEFAULT_TOKEN_CACHE),
    )
    # The Notion token is a secret too, so the environment wins here as well.
    notion_token = os.environ.get("NOTION_TOKEN") or raw_notion.get("token", "")
    notion = NotionConfig(
        token="" if is_placeholder(notion_token) else notion_token,
        database_id=os.environ.get("NOTION_DATABASE_ID")
        or raw_notion.get("database_id", ""),
        page_title_prefix=raw_notion.get("page_title_prefix", "Financial Report"),
    )
    # The AI key is optional - an unfilled placeholder just means no analysis,
    # which must not stop the report.
    ai_key = os.environ.get("GOOGLE_AI_API_KEY") or raw_ai.get("api_key")
    analyst = AnalystConfig(
        api_key=None if is_placeholder(ai_key) else ai_key,
        model=raw_ai.get("model") or DEFAULT_AI_MODEL,
        thinking_level=raw_ai.get("thinking_level") or DEFAULT_THINKING_LEVEL,
        universe_review=_as_bool(raw_ai.get("universe_review"), True),
        max_vetoes=_as_int(raw_ai.get("max_vetoes"), 3),
        max_candidates=_as_int(raw_ai.get("max_candidates"), 5),
        veto_ttl_days=_as_int(raw_ai.get("veto_ttl_days"), 7),
    )

    return AppConfig(
        toss=toss,
        notion=notion,
        analyst=analyst,
        manual_holdings=_parse_manual_holdings(raw_portfolio),
        savings_plans=_parse_savings_plans(raw_portfolio),
        news_keywords=list(raw_news.get("keywords") or []),
        trading=_parse_trading(raw_trading),
    )
