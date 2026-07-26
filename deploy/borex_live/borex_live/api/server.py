from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import unquote

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse

_service: Any = None
_static = Path(__file__).resolve().parent.parent / "static"


def bind_service(service: Any) -> None:
    global _service
    _service = service


def create_app() -> FastAPI:
    app = FastAPI(title="Borex Live")

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(_static / "live.html")

    @app.get("/candles")
    def candles_page() -> FileResponse:
        return FileResponse(_static / "candles.html")

    @app.get("/api/status")
    def status():
        if _service is None:
            raise HTTPException(503, "Service not started")
        return _service.runtime

    @app.get("/api/dashboard")
    def dashboard():
        if _service is None:
            raise HTTPException(503, "Service not started")
        try:
            return _service.dashboard_payload()
        except Exception as exc:
            raise HTTPException(500, f"dashboard failed: {exc}") from exc

    @app.get("/api/live-candles")
    def live_candles_index():
        """List symbols that have live MT5 candles (excludes warmup)."""
        if _service is None:
            raise HTTPException(503, "Service not started")
        return _service.live_candles_payload()

    @app.get("/api/live-candles/{symbol:path}")
    def live_candles_symbol(symbol: str):
        """OHLCV for one symbol — only bars ingested live from MT5."""
        if _service is None:
            raise HTTPException(503, "Service not started")
        sym = unquote(symbol)
        payload = _service.live_candles_payload(sym)
        if payload["count"] == 0 and sym not in getattr(_service, "live_candles_by_symbol", {}):
            # Still return empty series for known universe symbols
            universe = (_service.runtime or {}).get("symbols") or []
            if sym not in universe and sym not in _service.candles_by_symbol:
                raise HTTPException(404, f"Unknown symbol {sym}")
        return payload

    @app.post("/api/tick")
    def tick():
        if _service is None:
            raise HTTPException(503, "Service not started")
        return _service.process_once()

    return app
