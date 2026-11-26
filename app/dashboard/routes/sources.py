from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import reload_settings
from app.core.secrets_store import SECRET_FIELDS, propagate_settings, write_env_value
from app.core.security import secret_state
from app.dashboard.deps import audit, csrf_check, flash, render, require, services, tr
from app.database.database import get_session
from app.database.models import Admin, ProviderConfig

router = APIRouter()


@router.get("/sources")
async def sources_page(request: Request, admin: Admin = Depends(require()), session: AsyncSession = Depends(get_session)):
    svc = services(request)
    rows = {r.name: r for r in (await session.execute(select(ProviderConfig))).scalars()}
    items = []
    for name, p in svc.providers.providers.items():
        key_set = secret_state(getattr(svc.settings, SECRET_FIELDS[p.key_env][0], "")) if p.key_env in SECRET_FIELDS else None
        items.append({"name": name, "desc": p.description, "cfg": rows.get(name), "configured": p.is_configured(),
                      "hint": p.config_hint(), "key_env": p.key_env, "key_set": key_set, "key_required": p.key_required,
                      "supports_search": p.supports_search})
    items.sort(key=lambda i: i["cfg"].priority if i["cfg"] else 999)
    return render(request, "sources.html", admin, nav="sources", items=items, secret_fields=SECRET_FIELDS)


@router.post("/sources/{name}/update", dependencies=[Depends(csrf_check)])
async def update_source(name: str, request: Request, enabled: str = Form(""), priority: str = Form("100"),
                        admin: Admin = Depends(require("admin")), session: AsyncSession = Depends(get_session)):
    svc = services(request)
    row = await session.get(ProviderConfig, name)
    if row is None or name not in svc.providers.providers:
        raise HTTPException(404)
    if not priority.isdigit() or not 1 <= int(priority) <= 999:
        flash(request, tr(request, admin, "err_invalid_number"), "err")
        return RedirectResponse("/sources", 303)
    row.enabled, row.priority = enabled == "1", int(priority)
    svc.providers.config[name] = {"enabled": row.enabled, "priority": row.priority}
    await audit(session, request, admin, "source_updated", name, f"enabled={row.enabled} priority={row.priority}")
    flash(request, tr(request, admin, "msg_saved"))
    return RedirectResponse("/sources", 303)


@router.post("/sources/{name}/test", dependencies=[Depends(csrf_check)])
async def test_source(name: str, request: Request, admin: Admin = Depends(require("admin"))):
    svc = services(request)
    if name not in svc.providers.providers:
        raise HTTPException(404)
    ok, ms, err = await svc.providers.check(name)
    msg = tr(request, admin, "msg_source_ok", name=name, ms=ms) if ok else tr(request, admin, "msg_source_fail", name=name, error=err or "")
    flash(request, msg, "ok" if ok else "err")
    return RedirectResponse("/sources", 303)


@router.post("/sources/key", dependencies=[Depends(csrf_check)])
async def set_key(request: Request, env_name: str = Form(""), value: str = Form(""),
                  admin: Admin = Depends(require("superadmin")), session: AsyncSession = Depends(get_session)):
    """Write-only: stores the value in .env; it is never echoed back or logged."""
    svc = services(request)
    if env_name not in SECRET_FIELDS or "\n" in value or "\r" in value or len(value) > 512:
        flash(request, tr(request, admin, "err_invalid_value"), "err")
        return RedirectResponse("/sources", 303)
    write_env_value(env_name, value.strip())
    svc.settings = reload_settings()  # new Settings object; propagate to components that hold it
    propagate_settings(svc)
    await audit(session, request, admin, "secret_updated", env_name, "value not logged")
    flash(request, tr(request, admin, "msg_secret_saved", name=env_name))
    if env_name == "BALE_BOT_TOKEN":
        await svc.runner.start()
    return RedirectResponse("/sources" if env_name != "BALE_BOT_TOKEN" else "/settings", 303)
