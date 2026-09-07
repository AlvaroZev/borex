from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

_service: Any = None
_static = Path(__file__).resolve().parent / "static"


def bind_service(service: Any) -> None:
    global _service
    _service = service


def create_app() -> FastAPI:
    app = FastAPI(title="Borex Mirror")

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(_static / "mirror.html")

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

    @app.post("/api/tick")
    def tick():
        if _service is None:
            raise HTTPException(503, "Service not started")
        return _service.process_once()

    return app
