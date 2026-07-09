from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from borex.viewer.context import ViewerSession, confirmation_stats

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

    @app.get("/analysis")
    def analysis_page() -> FileResponse:
        return FileResponse(static_dir / "analysis.html")

    @app.get("/api/session")
    def api_session():
        s = get_session()
        return {
            "symbol": s.symbol,
            "timeframe": s.timeframe,
            "strategy": s.strategy_name,
            "leverage": s.leverage,
            "inversed": s.inversed,
            "tp_fraction": s.tp_fraction,
            "summary": s.summary_text,
            "total_return_pct": s.total_return_pct,
            "win_rate": s.win_rate,
            "total_trades": s.total_trades,
            "confirmation_stats": confirmation_stats(s.trades),
            "trades": s.trade_summaries(),
        }

    @app.get("/api/trades/{trade_id}/chart")
    def api_trade_chart(trade_id: int):
        s = get_session()
        try:
            return s.trade_chart(trade_id)
        except IndexError:
            raise HTTPException(status_code=404, detail="Trade not found") from None

    @app.get("/api/analysis/overview")
    def api_analysis_overview():
        s = get_session()
        if s.analysis is None:
            raise HTTPException(
                status_code=404,
                detail="Analysis available only for AlexG3/AlexG4/AlexG5 multi-market sessions",
            )
        payload = s.analysis.overview()
        payload["strategy"] = s.strategy_name
        payload["timeframe"] = s.timeframe
        payload["summary"] = s.summary_text
        return payload

    @app.get("/api/analysis/markets")
    def api_analysis_markets(show_aoi: bool = True):
        s = get_session()
        if s.analysis is None or not s.candles_by_symbol:
            raise HTTPException(
                status_code=404,
                detail="Analysis available only for AlexG3/AlexG4/AlexG5 multi-market sessions",
            )
        markets = []
        for sym in s.analysis.symbols:
            candles = (s.candles_by_symbol or {}).get(sym)
            markets.append(
                s.analysis.market_chart(sym, candles, show_aoi=show_aoi)
            )
        return {
            "symbols": s.analysis.symbols,
            "master_symbol": s.analysis.master_symbol,
            "timeline_start": (
                s.analysis.master_timeline_unix[0]
                if s.analysis.master_timeline_unix
                else None
            ),
            "timeline_end": (
                s.analysis.master_timeline_unix[-1]
                if s.analysis.master_timeline_unix
                else None
            ),
            "markets": markets,
        }

    if static_dir.is_dir():
        app.mount("/static", StaticFiles(directory=static_dir), name="static")

    return app
