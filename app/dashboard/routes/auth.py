from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.localization import available_languages, t
from app.core.security import RateLimiter, hash_password, verify_password
from app.dashboard.deps import audit, client_ip, csrf_check, csrf_token, render, safe_back
from app.database.database import get_session
from app.database.models import Admin, utcnow

router = APIRouter()
_ip_limiter = RateLimiter(10, 300)
MAX_FAILED, LOCK_MINUTES = 5, 10
_DUMMY_HASH = hash_password("scholarbot-dummy-password")


@router.get("/login")
async def login_page(request: Request):
    if request.session.get("admin_id"):
        return RedirectResponse("/", 303)
    return render(request, "login.html", None, error=None)


@router.post("/login", dependencies=[Depends(csrf_check)])
async def login(request: Request, username: str = Form(""), password: str = Form(""),
                session: AsyncSession = Depends(get_session)):
    lang = request.session.get("lang", "fa")
    ip = client_ip(request)

    def fail(key: str, status: int = 401):
        return render(request, "login.html", None, status_code=status, error=t(lang, key))

    if not _ip_limiter.allow(ip):
        return fail("err_login_rate", 429)
    username = username.strip()[:64]
    admin = (await session.execute(select(Admin).where(Admin.username == username))).scalar_one_or_none()
    if admin is None:
        verify_password(_DUMMY_HASH, password)  # keep timing similar for unknown users
        await audit(session, request, None, "login_failed", username)
        return fail("err_login_invalid")
    if admin.locked_until and admin.locked_until > utcnow():
        return fail("err_login_locked", 429)
    if not admin.is_active or not verify_password(admin.password_hash, password):
        admin.failed_attempts += 1
        if admin.failed_attempts >= MAX_FAILED:
            admin.locked_until, admin.failed_attempts = utcnow() + timedelta(minutes=LOCK_MINUTES), 0
        await audit(session, request, admin, "login_failed", admin.username)
        await session.commit()
        return fail("err_login_invalid")
    admin.failed_attempts, admin.locked_until, admin.last_login_at = 0, None, utcnow()
    lang_pref, theme_pref = admin.language, admin.theme
    request.session.clear()  # fresh session id contents on login (prevents fixation)
    request.session.update({"admin_id": admin.id, "lang": lang_pref, "theme": theme_pref})
    csrf_token(request)
    await audit(session, request, admin, "login", admin.username)
    _ip_limiter.reset(ip)
    return RedirectResponse("/", 303)


@router.post("/logout", dependencies=[Depends(csrf_check)])
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", 303)


@router.post("/prefs", dependencies=[Depends(csrf_check)])
async def prefs(request: Request, lang: str = Form(""), theme: str = Form(""),
                session: AsyncSession = Depends(get_session)):
    admin_id = request.session.get("admin_id")
    admin = await session.get(Admin, admin_id) if admin_id else None
    if lang in available_languages():
        request.session["lang"] = lang
        if admin:
            admin.language = lang
    if theme in ("light", "dark"):
        request.session["theme"] = theme
        if admin:
            admin.theme = theme
    return RedirectResponse(safe_back(request), 303)
