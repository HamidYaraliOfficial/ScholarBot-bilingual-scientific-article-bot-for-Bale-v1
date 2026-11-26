from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import stats
from app.dashboard.deps import render, require, services
from app.database.database import get_session
from app.database.models import Admin, ErrorEvent

router = APIRouter()


@router.get("/")
async def index(request: Request, admin: Admin = Depends(require()), session: AsyncSession = Depends(get_session)):
    svc = services(request)
    errors = (await session.execute(select(ErrorEvent).order_by(ErrorEvent.created_at.desc()).limit(8))).scalars().all()
    return render(request, "dashboard.html", admin, nav="dashboard", st=await stats.overview(session, svc), errors=errors)


@router.get("/dashboard/cards")
async def cards(request: Request, admin: Admin = Depends(require()), session: AsyncSession = Depends(get_session)):
    return render(request, "_cards.html", admin, st=await stats.overview(session, services(request)))


@router.get("/api/stats/charts")
async def chart_data(admin: Admin = Depends(require()), session: AsyncSession = Depends(get_session)):
    return JSONResponse(await stats.charts(session))
