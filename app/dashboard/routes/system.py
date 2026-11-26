"""Reports, system logs, settings, backup & restore."""
from __future__ import annotations

import csv
import io
import json
from typing import Optional
from collections import Counter
from datetime import timedelta

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging_config import set_level
from app.core.runtime_settings import DEFINITIONS, runtime
from app.core.secrets_store import SECRET_FIELDS
from app.core.security import secret_state
from app.dashboard.deps import audit, csrf_check, flash, render, require, services, tr
from app.database.database import get_session, session_scope
from app.database.models import Admin, AuditLog, DownloadJob, ErrorEvent, ProviderConfig, utcnow
from app.services import backup as backup_svc

router = APIRouter()


# --- reports ---------------------------------------------------------------------------------------------------
@router.get("/reports")
async def reports(request: Request, kind: str = "", admin: Admin = Depends(require()),
                  session: AsyncSession = Depends(get_session)):
    since = utcnow() - timedelta(days=7)
    by_kind = dict((await session.execute(select(ErrorEvent.kind, func.count()).where(ErrorEvent.created_at >= since)
                                          .group_by(ErrorEvent.kind))).all())
    by_source = (await session.execute(select(ErrorEvent.kind, ErrorEvent.source, func.count()).where(ErrorEvent.created_at >= since)
                                       .group_by(ErrorEvent.kind, ErrorEvent.source).order_by(func.count().desc()).limit(15))).all()
    stmt = select(ErrorEvent).order_by(ErrorEvent.created_at.desc()).limit(100)
    if kind in ("bot", "provider", "delivery", "system"):
        stmt = select(ErrorEvent).where(ErrorEvent.kind == kind).order_by(ErrorEvent.created_at.desc()).limit(100)
    events = (await session.execute(stmt)).scalars().all()
    failures = Counter(code for (code,) in (await session.execute(select(DownloadJob.error_code).where(
        DownloadJob.created_at >= since, DownloadJob.error_code.is_not(None)))).all())
    providers = (await session.execute(select(ProviderConfig).order_by(ProviderConfig.priority))).scalars().all()
    return render(request, "reports.html", admin, nav="reports", by_kind=by_kind, by_source=by_source, events=events,
                  kind=kind, failures=failures.most_common(10), providers=providers)


@router.get("/reports/errors.csv")
async def errors_csv(admin: Admin = Depends(require()), session: AsyncSession = Depends(get_session)):
    rows = (await session.execute(select(ErrorEvent).order_by(ErrorEvent.created_at.desc()).limit(5000))).scalars().all()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["time_utc", "kind", "source", "message"])
    for e in rows:
        msg = e.message if e.message[:1] not in "=+-@" else "'" + e.message
        w.writerow([e.created_at.isoformat(sep=" ", timespec="seconds"), e.kind, e.source, msg])
    return Response(buf.getvalue().encode("utf-8-sig"), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="scholarbot_errors.csv"'})


# --- logs -------------------------------------------------------------------------------------------------------
def _tail(path, lines: int, level: str) -> list[dict]:
    if not path.exists():
        return []
    with path.open("rb") as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - 400_000))
        chunk = f.read().decode("utf-8", "replace").splitlines()
    out = []
    for line in reversed(chunk):
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if level and rec.get("level") != level:
            continue
        out.append(rec)
        if len(out) >= lines:
            break
    return out


@router.get("/logs")
async def logs_page(request: Request, level: str = "", partial: int = 0, admin: Admin = Depends(require("admin"))):
    svc = services(request)
    recs = _tail(svc.settings.log_dir / "app.log", 200, level if level in ("DEBUG", "INFO", "WARNING", "ERROR") else "")
    return render(request, "_logs_table.html" if partial else "logs.html", admin, nav="logs", records=recs, level=level)


# --- settings ---------------------------------------------------------------------------------------------------
@router.get("/settings")
async def settings_page(request: Request, admin: Admin = Depends(require()), session: AsyncSession = Depends(get_session)):
    svc = services(request)
    secrets = [{"env": env, "label": label, "is_set": secret_state(getattr(svc.settings, attr, ""))}
               for env, (attr, label) in SECRET_FIELDS.items()]
    return render(request, "settings.html", admin, nav="settings", defs=DEFINITIONS, values=runtime.all(), secrets=secrets,
                  api_base=svc.settings.bale_api_base, usage=svc.storage.usage(), backups=backup_svc.list_backups(svc.settings), bot=svc.runner.status,
                  db_kind=svc.settings.database_url.split(":", 1)[0].split("+")[0], storage_dir=str(svc.settings.storage_dir),
                  last_cleanup=svc.maintenance.last_cleanup_result)


@router.post("/settings", dependencies=[Depends(csrf_check)])
async def save_settings(request: Request, admin: Admin = Depends(require("superadmin")),
                        session: AsyncSession = Depends(get_session)):
    svc = services(request)
    form = await request.form()
    changed, errors = [], []
    for key, defn in DEFINITIONS.items():
        if defn.type == "bool":
            raw = "true" if form.get(key) == "on" else "false"
        elif key in form:
            raw = form[key]
        else:
            continue
        try:
            before = runtime.get(key)
            after = await runtime.set(session, key, raw)
            if before != after:
                changed.append(key)
        except (ValueError, TypeError):
            errors.append(key)
    if "concurrency" in changed:
        await svc.tasks.resize(runtime.get("concurrency"))
    if "log_level" in changed:
        set_level(runtime.get("log_level"))
    await audit(session, request, admin, "settings_changed", ",".join(changed) or None)
    if errors:
        flash(request, tr(request, admin, "err_invalid_value") + ": " + ", ".join(errors), "err")
    else:
        flash(request, tr(request, admin, "msg_saved"))
    return RedirectResponse("/settings", 303)


@router.post("/settings/cleanup", dependencies=[Depends(csrf_check)])
async def cleanup_now(request: Request, admin: Admin = Depends(require("admin")), session: AsyncSession = Depends(get_session)):
    res = await services(request).maintenance.run_cleanup()
    await audit(session, request, admin, "cleanup_run", json.dumps(res))
    flash(request, tr(request, admin, "msg_cleanup_done", n=res["temp_files_removed"]))
    return RedirectResponse("/settings", 303)


# --- backup / restore ---------------------------------------------------------------------------------------------
@router.post("/backups/create", dependencies=[Depends(csrf_check)])
async def create_backup(request: Request, admin: Admin = Depends(require("admin")), session: AsyncSession = Depends(get_session)):
    path = await backup_svc.create_backup(services(request).settings)
    await audit(session, request, admin, "backup_created", path.name)
    flash(request, tr(request, admin, "msg_backup_created", name=path.name))
    return RedirectResponse("/settings#backups", 303)


@router.get("/backups/{name}")
async def download_backup(name: str, request: Request, admin: Admin = Depends(require("admin")),
                          session: AsyncSession = Depends(get_session)):
    p = backup_svc.backup_path(services(request).settings, name)
    if p is None:
        raise HTTPException(404)
    await audit(session, request, admin, "backup_downloaded", name)
    return FileResponse(p, media_type="application/zip", filename=name)


@router.post("/backups/restore", dependencies=[Depends(csrf_check)])
async def restore_backup(request: Request, name: str = Form(""), confirm: str = Form(""), upload: Optional[UploadFile] = File(None),
                         admin: Admin = Depends(require("superadmin")), session: AsyncSession = Depends(get_session)):
    svc = services(request)
    if confirm != "yes":
        flash(request, tr(request, admin, "err_confirm_required"), "err")
        return RedirectResponse("/settings#backups", 303)
    path = backup_svc.backup_path(svc.settings, name) if name else None
    tmp = None
    if path is None and upload is not None and upload.filename:
        tmp = svc.settings.temp_dir / f"upload_{utcnow().timestamp():.0f}.zip"
        size = 0
        with tmp.open("wb") as out:
            while chunk := await upload.read(1 << 20):
                size += len(chunk)
                if size > 2 * 1024**3:
                    out.close(); tmp.unlink(missing_ok=True)
                    flash(request, tr(request, admin, "err_file_too_large"), "err")
                    return RedirectResponse("/settings#backups", 303)
                out.write(chunk)
        path = tmp
    if path is None:
        flash(request, tr(request, admin, "err_invalid_value"), "err")
        return RedirectResponse("/settings#backups", 303)
    try:
        await session.commit()  # release our own transaction before the restore rewrites tables
        res = await backup_svc.restore_backup(svc.settings, path)
    except backup_svc.BackupError as exc:
        flash(request, f"{tr(request, admin, 'err_restore_failed')}: {exc}", "err")
        return RedirectResponse("/settings#backups", 303)
    finally:
        if tmp:
            tmp.unlink(missing_ok=True)
    async with session_scope() as s:
        await runtime.load(s)
        s.add(AuditLog(admin_username=admin.username, action="backup_restored", target=path.name))
    await svc.providers.load()
    flash(request, tr(request, admin, "msg_restore_done", n=res.get("articles", 0)))
    return RedirectResponse("/settings#backups", 303)
