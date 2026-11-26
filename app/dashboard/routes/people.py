"""Users, administrators, download history."""
from __future__ import annotations

import math
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import String, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.runtime_settings import runtime
from app.core.security import hash_password, password_problem
from app.dashboard.deps import LEVELS, audit, csrf_check, flash, render, require, safe_back, tr
from app.database.database import get_session
from app.database.models import Admin, AuditLog, DownloadJob, User, utcnow

router = APIRouter()
JOB_STATUSES = ("queued", "processing", "awaiting_choice", "found", "sent", "not_found", "failed")


# --- download history ---------------------------------------------------------------------------------
@router.get("/history")
async def history(request: Request, status: str = "", q: str = "", page: int = 1, partial: int = 0,
                  admin: Admin = Depends(require()), session: AsyncSession = Depends(get_session)):
    page, per_page = max(1, page), 30
    stmt = select(DownloadJob)
    count = select(func.count(DownloadJob.id))
    if status in JOB_STATUSES:
        stmt, count = stmt.where(DownloadJob.status == status), count.where(DownloadJob.status == status)
    if q.strip():
        like = f"%{q.strip().lower()[:100]}%"
        stmt, count = (stmt.where(func.lower(DownloadJob.query_text).like(like)),
                       count.where(func.lower(DownloadJob.query_text).like(like)))
    total = (await session.execute(count)).scalar_one()
    jobs = (await session.execute(stmt.order_by(DownloadJob.created_at.desc()).limit(per_page).offset((page - 1) * per_page))).unique().scalars().all()
    ctx = dict(jobs=jobs, total=total, page=page, pages=max(1, math.ceil(total / per_page)), status=status, q=q,
               statuses=JOB_STATUSES, qs=lambda **o: urlencode({k: v for k, v in {"status": status, "q": q, **o}.items() if v not in ("", None)}))
    return render(request, "_history_table.html" if partial else "history.html", admin, nav="history", **ctx)


# --- users ----------------------------------------------------------------------------------------------
@router.get("/users")
async def users_page(request: Request, q: str = "", page: int = 1, admin: Admin = Depends(require()),
                     session: AsyncSession = Depends(get_session)):
    page, per_page = max(1, page), 30
    cnt = func.count(DownloadJob.id).label("requests")
    stmt = select(User, cnt).outerjoin(DownloadJob, DownloadJob.user_id == User.id).group_by(User.id)
    total_stmt = select(func.count(User.id))
    if q.strip():
        like = f"%{q.strip().lower()[:80]}%"
        cond = (func.lower(func.coalesce(User.username, "")).like(like) | func.lower(func.coalesce(User.first_name, "")).like(like)
                | cast(User.bale_id, String).like(like))
        stmt, total_stmt = stmt.where(cond), total_stmt.where(cond)
    total = (await session.execute(total_stmt)).scalar_one()
    rows = (await session.execute(stmt.order_by(User.last_seen_at.desc()).limit(per_page).offset((page - 1) * per_page))).all()
    return render(request, "users.html", admin, nav="users", rows=rows, q=q, page=page, total=total,
                  pages=max(1, math.ceil(total / per_page)), default_quota=runtime.get("user_daily_quota"), now=utcnow())


@router.get("/users/{user_id}")
async def user_detail(user_id: int, request: Request, admin: Admin = Depends(require()),
                      session: AsyncSession = Depends(get_session)):
    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(404)
    jobs = (await session.execute(select(DownloadJob).where(DownloadJob.user_id == user_id)
                                  .order_by(DownloadJob.created_at.desc()).limit(50))).unique().scalars().all()
    return render(request, "_user_detail.html", admin, u=user, jobs=jobs)


@router.post("/users/{user_id}/block", dependencies=[Depends(csrf_check)])
async def block_user(user_id: int, request: Request, block: int = Form(1), admin: Admin = Depends(require("admin")),
                     session: AsyncSession = Depends(get_session)):
    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(404)
    user.is_blocked = bool(block)
    await audit(session, request, admin, "user_blocked" if block else "user_unblocked", f"bale:{user.bale_id}")
    flash(request, tr(request, admin, "msg_user_blocked" if block else "msg_user_unblocked"))
    return RedirectResponse(safe_back(request, "/users"), 303)


@router.post("/users/{user_id}/quota", dependencies=[Depends(csrf_check)])
async def set_quota(user_id: int, request: Request, quota: str = Form(""), admin: Admin = Depends(require("admin")),
                    session: AsyncSession = Depends(get_session)):
    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(404)
    quota = quota.strip()
    if quota == "":
        user.quota_daily = None
    elif quota.isdigit() and 0 <= int(quota) <= 100000:
        user.quota_daily = int(quota)
    else:
        flash(request, tr(request, admin, "err_invalid_number"), "err")
        return RedirectResponse("/users", 303)
    await audit(session, request, admin, "user_quota_changed", f"bale:{user.bale_id}", quota or "default")
    flash(request, tr(request, admin, "msg_saved"))
    return RedirectResponse("/users", 303)


# --- administrators --------------------------------------------------------------------------------------
@router.get("/admins")
async def admins_page(request: Request, admin: Admin = Depends(require("superadmin")),
                      session: AsyncSession = Depends(get_session)):
    admins = (await session.execute(select(Admin).order_by(Admin.created_at))).scalars().all()
    logs = (await session.execute(select(AuditLog).order_by(AuditLog.created_at.desc()).limit(100))).scalars().all()
    return render(request, "admins.html", admin, nav="admins", admins=admins, logs=logs, roles=list(LEVELS))


@router.post("/admins/create", dependencies=[Depends(csrf_check)])
async def create_admin(request: Request, username: str = Form(""), password: str = Form(""), role: str = Form("admin"),
                       admin: Admin = Depends(require("superadmin")), session: AsyncSession = Depends(get_session)):
    username = username.strip()
    problem = password_problem(password)
    if not (3 <= len(username) <= 64) or not username.replace("_", "").replace("-", "").replace(".", "").isalnum() or role not in LEVELS:
        flash(request, tr(request, admin, "err_invalid_username"), "err")
    elif problem:
        flash(request, tr(request, admin, problem), "err")
    elif (await session.execute(select(Admin).where(Admin.username == username))).scalar_one_or_none():
        flash(request, tr(request, admin, "err_username_taken"), "err")
    else:
        session.add(Admin(username=username, password_hash=hash_password(password), role=role))
        await audit(session, request, admin, "admin_created", username, f"role={role}")
        flash(request, tr(request, admin, "msg_saved"))
    return RedirectResponse("/admins", 303)


@router.post("/admins/{admin_id}/update", dependencies=[Depends(csrf_check)])
async def update_admin(admin_id: int, request: Request, role: str = Form(""), active: str = Form(""), password: str = Form(""),
                       admin: Admin = Depends(require("superadmin")), session: AsyncSession = Depends(get_session)):
    target = await session.get(Admin, admin_id)
    if target is None:
        raise HTTPException(404)
    is_active = active == "1"
    supers = (await session.execute(select(func.count(Admin.id)).where(Admin.role == "superadmin", Admin.is_active.is_(True)))).scalar_one()
    demoting = target.role == "superadmin" and target.is_active and (role != "superadmin" or not is_active)
    if demoting and supers <= 1:
        flash(request, tr(request, admin, "err_last_superadmin"), "err")
        return RedirectResponse("/admins", 303)
    if role in LEVELS:
        target.role = role
    target.is_active = is_active
    if password:
        problem = password_problem(password)
        if problem:
            flash(request, tr(request, admin, problem), "err")
            return RedirectResponse("/admins", 303)
        target.password_hash, target.failed_attempts, target.locked_until = hash_password(password), 0, None
    await audit(session, request, admin, "admin_updated", target.username, f"role={target.role} active={target.is_active} pw={'yes' if password else 'no'}")
    flash(request, tr(request, admin, "msg_saved"))
    return RedirectResponse("/admins", 303)
