from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from borex.viewer.context import ViewerSession

_session: ViewerSession | None = None


def set_session(session: ViewerSession) -> None:
    global _session
    _session = session


def get_session() -> ViewerSession:
    if _session is None:
        raise HTTPException(status_code=503, detail="No backtest session loaded")
    return _session


def create_app(static_dir: Path) -> FastAPI:
    app = FastAPI(title="Borex Trade Viewer")

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(static_dir / "index.html")

    @app.get("/api/session")
    def api_session():
        s = get_session()
        return {
            "symbol": s.symbol,
            "timeframe": s.timeframe,
            "strategy": s.strategy_name,
            "leverage": s.leverage,
            "summary": s.summary_text,
            "total_return_pct": s.total_return_pct,
            "win_rate": s.win_rate,
            "total_trades": s.total_trades,
            "trades": s.trade_summaries(),
        }

    @app.get("/api/trades/{trade_id}/chart")
    def api_trade_chart(trade_id: int):
        s = get_session()
        try:
            return s.trade_chart(trade_id)
        except IndexError:
            raise HTTPException(status_code=404, detail="Trade not found") from None

    if static_dir.is_dir():
        app.mount("/static", StaticFiles(directory=static_dir), name="static")

    return app
