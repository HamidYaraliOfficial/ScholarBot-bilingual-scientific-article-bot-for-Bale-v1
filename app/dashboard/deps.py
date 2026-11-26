"""Dashboard dependencies: authentication, roles, CSRF, rendering and audit helpers."""
from __future__ import annotations

import json
from datetime import timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.localization import DEFAULT_LANG, available_languages, is_rtl, normalize_lang, t
from app.core.security import new_token, tokens_match
from app.database.database import get_session
from app.database.models import Admin, AuditLog
from app.services.container import Services

LEVELS = {"viewer": 1, "admin": 2, "superadmin": 3}
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _iso(dt) -> str:
    return dt.replace(tzinfo=timezone.utc).isoformat() if dt else ""


def _filesize(n) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return ""


templates.env.filters["iso"] = _iso
templates.env.filters["filesize"] = _filesize
templates.env.filters["tojson_attr"] = lambda v: json.dumps(v, ensure_ascii=False)


def services(request: Request) -> Services:
    return request.app.state.services


def is_htmx(request: Request) -> bool:
    return request.headers.get("hx-request") == "true"


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "?"


def csrf_token(request: Request) -> str:
    tok = request.session.get("csrf")
    if not tok:
        tok = request.session["csrf"] = new_token()
    return tok


async def csrf_check(request: Request) -> None:
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        sent = request.headers.get("x-csrf-token")
        if not sent:
            sent = (await request.form()).get("csrf_token")
        if not tokens_match(str(sent or ""), request.session.get("csrf")):
            raise HTTPException(403, "CSRF token missing or invalid")


def _redirect_login(request: Request) -> HTTPException:
    headers = {"HX-Redirect": "/login"} if is_htmx(request) else {"Location": "/login"}
    return HTTPException(401 if is_htmx(request) else 303, "login required", headers=headers)


def require(role: str = "viewer"):
    async def dep(request: Request, session: AsyncSession = Depends(get_session)) -> Admin:
        admin_id = request.session.get("admin_id")
        admin = await session.get(Admin, admin_id) if admin_id else None
        if admin is None or not admin.is_active:
            request.session.pop("admin_id", None)
            raise _redirect_login(request)
        if LEVELS[admin.role] < LEVELS[role]:
            raise HTTPException(403, "insufficient permissions")
        request.state.admin = admin
        return admin
    return dep


def page_context(request: Request, admin: Admin | None = None, **extra: Any) -> dict[str, Any]:
    lang = normalize_lang(admin.language if admin else request.session.get("lang", DEFAULT_LANG))
    theme = (admin.theme if admin else request.session.get("theme", "light"))
    flash = request.session.pop("flash", [])
    return {"request": request, "admin": admin, "lang": lang, "dir": "rtl" if is_rtl(lang) else "ltr", "theme": theme,
            "csrf": csrf_token(request), "_": lambda key, **kw: t(lang, key, **kw), "flash": flash,
            "languages": available_languages(), "LEVELS": LEVELS, **extra}


def render(request: Request, name: str, admin: Admin | None, status_code: int = 200, **extra: Any) -> HTMLResponse:
    return templates.TemplateResponse(request, name, page_context(request, admin, **extra), status_code=status_code)


def flash(request: Request, message: str, kind: str = "ok") -> None:
    # reassign (not mutate in place) so Starlette notices the session changed and re-issues the cookie
    request.session["flash"] = [*request.session.get("flash", []), {"msg": message, "kind": kind}]


def toast_headers(message: str, kind: str = "ok") -> dict[str, str]:
    return {"HX-Trigger": json.dumps({"toast": {"msg": message, "kind": kind}}, ensure_ascii=True)}


def tr(request: Request, admin: Admin, key: str, **kw) -> str:
    return t(normalize_lang(admin.language), key, **kw)


def json_result(ok: bool, message: str, status: int = 200, **extra: Any) -> JSONResponse:
    return JSONResponse({"ok": ok, "message": message, **extra}, status_code=status)


async def audit(session: AsyncSession, request: Request, admin: Admin | None, action: str, target: str | None = None,
                detail: str | None = None) -> None:
    session.add(AuditLog(admin_username=admin.username if admin else None, action=action, target=target,
                         detail=(detail or "")[:2000] or None, ip=client_ip(request)))


def safe_back(request: Request, default: str = "/") -> str:
    """Same-origin path from the Referer header (never an external URL)."""
    ref = request.headers.get("referer")
    if ref:
        p = urlsplit(ref)
        if p.netloc == request.headers.get("host", ""):
            return p.path + (f"?{p.query}" if p.query else "")
    return default
