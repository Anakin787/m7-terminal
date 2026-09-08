"""FastAPI app serving the M7 Terminal dashboard.

Read-only apart from the kill switch (design 6 [12]) and cosmetic name
overrides. Bind to 127.0.0.1: this app can now stop the trading engine and it
has no authentication.

    uvicorn src.dashboard.api:app --host 127.0.0.1 --port 8000
"""

import os
from contextlib import asynccontextmanager

from fastapi import Body, FastAPI, Path, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from src.config import load_config
from src.dashboard.service import DashboardService

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

_service = None


@asynccontextmanager
async def lifespan(_app):
    yield
    if _service is not None:
        _service.close()


app = FastAPI(title="M7 Terminal", docs_url="/api/docs", lifespan=lifespan)


def get_service():
    global _service
    if _service is None:
        _service = DashboardService(load_config())
    return _service


@app.get("/api/overview")
def overview():
    return get_service().overview()


@app.get("/api/holdings")
def holdings():
    return get_service().holdings()


@app.put("/api/holdings/{symbol}/name")
def rename_holding(
    symbol: str = Path(pattern=r"^[A-Za-z0-9.\-]{1,20}$"),
    name: str = Body(embed=True, max_length=80),
):
    """Set the display name for a ticker; an empty name clears the override.

    Cosmetic metadata only - it never touches the account. Toss reports
    name == symbol for some tickers, which makes the table hard to read.
    """
    return {"symbol": symbol, "name": get_service().rename_symbol(symbol, name)}


@app.get("/api/history")
def history(range: str = Query("3M", pattern="^(1W|1M|3M|1Y|ALL)$")):
    return get_service().history(range)


@app.get("/api/allocation")
def allocation(by: str = Query("market", pattern="^(market|currency|bucket)$")):
    return get_service().allocation(by)


@app.get("/api/reports")
def reports(limit: int = Query(20, ge=1, le=100)):
    return get_service().reports(limit)


@app.get("/api/audit")
def audit(
    limit: int = Query(50, ge=1, le=200),
    category: str | None = Query(None, pattern="^(baseline|universe|strategies|strategy_params|limits|veto|candidate|kill_switch)$"),
):
    return get_service().audit(limit=limit, category=category)


@app.get("/api/trading/status")
def trading_status():
    return get_service().trading_status()


@app.get("/api/trading/activity")
def trading_activity(limit: int = Query(50, ge=1, le=200)):
    return get_service().trading_activity(limit=limit)


@app.post("/api/trading/kill-switch")
def set_kill_switch(
    active: bool = Body(embed=True),
    reason: str | None = Body(default=None, embed=True, max_length=200),
):
    """Engage or release the kill switch.

    This is the first endpoint that changes what the engine does, and the app
    still has no authentication - which is why it binds to 127.0.0.1 and why
    the only write it accepts is the one that *stops* trading (and its
    reversal). Nothing here can place an order.
    """
    return get_service().set_kill_switch(active, reason=reason)


@app.get("/api/health")
def health():
    return get_service().health()


@app.get("/api/settings")
def settings():
    return get_service().settings()


@app.get("/")
def index():
    # The page must never be cached: it carries the ?v= stamp that decides
    # which app.js the browser loads, so a stale copy pairs old markup with
    # new script and the view dies on an element that no longer exists.
    return FileResponse(
        os.path.join(STATIC_DIR, "index.html"),
        headers={"Cache-Control": "no-cache, must-revalidate"},
    )


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
