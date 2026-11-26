"""Small JSON API: health probe for monitors / start scripts."""
from __future__ import annotations

from fastapi import APIRouter, Request
from sqlalchemy import text

from app.database.database import session_scope

router = APIRouter(prefix="/api")


@router.get("/health")
async def health(request: Request):
    svc = request.app.state.services
    try:
        async with session_scope() as s:
            await s.execute(text("SELECT 1"))
        db = "ok"
    except Exception:  # noqa: BLE001
        db = "error"
    return {"status": "ok" if db == "ok" else "degraded", "database": db, "bot": svc.runner.status["state"],
            "queued": svc.tasks.queued, "active": svc.tasks.active}
