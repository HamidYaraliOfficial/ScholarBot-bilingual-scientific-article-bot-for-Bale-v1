from __future__ import annotations

import math
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.background import BackgroundTask

from app.core.security import safe_filename
from app.dashboard.deps import (audit, csrf_check, json_result, render, require, services, tr)
from app.database.database import get_session
from app.database.models import Admin, Delivery, DownloadJob
from app.database.repositories import articles as repo
from app.providers.registry import DEFAULT_PRIORITY
from app.services import exports

router = APIRouter()
PER_PAGE_CHOICES = (10, 25, 50, 100)
MAX_BULK = 5000


def read_filters(params, force_saved: bool = False) -> tuple[repo.ArticleFilters, dict]:
    def num(name: str, default: int | None = None) -> int | None:
        v = params.get(name, "")
        return int(v) if str(v).isdigit() else default

    f = repo.ArticleFilters(q=params.get("q", "").strip()[:200], doi=params.get("doi", "").strip()[:200],
                            author=params.get("author", "").strip()[:100], year=num("year"),
                            source=params.get("source", "")[:32], status=params.get("status", "")[:16],
                            saved="saved" if force_saved else params.get("saved", "")[:12], user_id=num("user"))
    per_page = num("per_page", 25)
    view = {"page": max(1, num("page", 1) or 1), "per_page": per_page if per_page in PER_PAGE_CHOICES else 25,
            "sort": params.get("sort", "created_at"), "desc": params.get("dir", "desc") != "asc"}
    return f, view


async def _table_ctx(request: Request, session: AsyncSession, force_saved: bool) -> dict:
    f, v = read_filters(request.query_params, force_saved)
    items, total = await repo.query_articles(session, f, sort=v["sort"], desc=v["desc"], page=v["page"], per_page=v["per_page"])
    pages = max(1, math.ceil(total / v["per_page"]))
    base = {**f.as_query(), "per_page": v["per_page"], "sort": v["sort"], "dir": "desc" if v["desc"] else "asc"}
    if force_saved:
        base.pop("saved", None)

    def qs(**over) -> str:
        q = {**base, **over}
        return urlencode({k: val for k, val in q.items() if val not in ("", None)})

    return {"items": items, "total": total, "page": v["page"], "pages": pages, "filters": f, "view": v, "qs": qs,
            "library": force_saved, "sources": list(DEFAULT_PRIORITY), "per_page_choices": PER_PAGE_CHOICES,
            "base_path": "/library" if force_saved else "/articles", "filter_query": urlencode(f.as_query())}


@router.get("/articles")
async def articles_page(request: Request, admin: Admin = Depends(require()), session: AsyncSession = Depends(get_session)):
    return render(request, "articles.html", admin, nav="articles", **await _table_ctx(request, session, False))


@router.get("/library")
async def library_page(request: Request, admin: Admin = Depends(require()), session: AsyncSession = Depends(get_session)):
    return render(request, "articles.html", admin, nav="library", **await _table_ctx(request, session, True))


@router.get("/articles/table")
async def articles_table(request: Request, admin: Admin = Depends(require()), session: AsyncSession = Depends(get_session)):
    return render(request, "_articles_table.html", admin, **await _table_ctx(request, session, request.query_params.get("library") == "1"))


@router.get("/articles/{article_id}")
async def article_detail(article_id: int, request: Request, admin: Admin = Depends(require()),
                         session: AsyncSession = Depends(get_session)):
    art = (await repo.get_many(session, [article_id]) or [None])[0]
    if art is None:
        raise HTTPException(404)
    jobs = (await session.execute(select(DownloadJob).where(DownloadJob.article_id == art.id)
                                  .order_by(DownloadJob.created_at.desc()).limit(25))).unique().scalars().all()
    deliveries = (await session.execute(select(Delivery).where(Delivery.article_id == art.id)
                                        .order_by(Delivery.sent_at.desc()).limit(25))).scalars().all()
    return render(request, "_article_detail.html", admin, a=art, jobs=jobs, deliveries=deliveries)


@router.get("/articles/{article_id}/file")
async def article_file(article_id: int, request: Request, download: int = 0, admin: Admin = Depends(require()),
                       session: AsyncSession = Depends(get_session)):
    art = (await repo.get_many(session, [article_id]) or [None])[0]
    if art is None or art.file is None:
        raise HTTPException(404)
    svc = services(request)
    try:
        path = svc.storage.abs_path(art.file.path, art.file.is_permanent)
    except ValueError:
        raise HTTPException(404)
    if not path.is_file():
        raise HTTPException(404)
    name = f"{safe_filename(art.doi or art.title, 80)}.pdf"
    disposition = "attachment" if download else "inline"
    return FileResponse(path, media_type="application/pdf", headers={"Content-Disposition": f'{disposition}; filename="{name}"',
                                                                     "X-Content-Type-Options": "nosniff"})


async def _selected_ids(request: Request, session: AsyncSession) -> tuple[list[int], dict]:
    form = await request.form()
    if form.get("all_filtered") == "1":
        f, _ = read_filters(form, force_saved=form.get("library") == "1")
        return await repo.ids_for_filters(session, f, MAX_BULK), dict(form)
    ids = [int(x) for x in form.getlist("ids") if str(x).isdigit()][:MAX_BULK]
    return ids, dict(form)


@router.post("/articles/bulk", dependencies=[Depends(csrf_check)])
async def bulk(request: Request, admin: Admin = Depends(require("admin")), session: AsyncSession = Depends(get_session)):
    svc = services(request)
    ids, form = await _selected_ids(request, session)
    if not ids:
        return json_result(False, tr(request, admin, "err_nothing_selected"), 400)
    action = form.get("action")
    arts = await repo.get_many(session, ids)
    if action == "save":
        r = await svc.library.save(session, arts, admin.username)
        await audit(session, request, admin, "articles_saved", f"{r['saved']} articles", str(ids[:50]))
        msg = tr(request, admin, "msg_bulk_saved", saved=r["saved"], already=r["already"], no_file=r["no_file"])
    elif action == "unsave":
        n = await svc.library.unsave(session, arts)
        await audit(session, request, admin, "articles_unsaved", f"{n} articles", str(ids[:50]))
        msg = tr(request, admin, "msg_bulk_unsaved", n=n)
    elif action == "delete":
        if form.get("confirm") != "yes":
            return json_result(False, tr(request, admin, "err_confirm_required"), 400)
        mode = form.get("mode") if form.get("mode") in ("info", "info_and_file") else "info"
        r = await svc.library.delete_articles(session, arts, mode)
        await audit(session, request, admin, "articles_deleted", f"{r['deleted']} articles, mode={mode}", str(ids[:50]))
        msg = tr(request, admin, "msg_bulk_deleted", deleted=r["deleted"], files=r["files_removed"], shared=r["shared_files_kept"])
    else:
        return json_result(False, "unknown action", 400)
    await session.commit()
    return json_result(True, msg, reload=True)


@router.post("/articles/export", dependencies=[Depends(csrf_check)])
async def export_articles(request: Request, admin: Admin = Depends(require()), session: AsyncSession = Depends(get_session)):
    ids, form = await _selected_ids(request, session)
    fmt = form.get("fmt", "")
    if fmt not in ("csv", "xlsx", "bibtex", "ris"):
        raise HTTPException(400, "unknown format")
    if not ids:
        return json_result(False, tr(request, admin, "err_nothing_selected"), 400)
    arts = await repo.get_many(session, ids)
    data, mime, ext = exports.export(arts, fmt)
    await audit(session, request, admin, "articles_exported", f"{len(arts)} articles as {fmt}")
    return Response(data, media_type=mime, headers={"Content-Disposition": f'attachment; filename="scholarbot_articles.{ext}"'})


@router.post("/articles/zip", dependencies=[Depends(csrf_check)])
async def zip_articles(request: Request, admin: Admin = Depends(require()), session: AsyncSession = Depends(get_session)):
    svc = services(request)
    ids, _ = await _selected_ids(request, session)
    if not ids:
        return json_result(False, tr(request, admin, "err_nothing_selected"), 400)
    path = await svc.library.zip_files(await repo.get_many(session, ids), f"scholarbot_pdfs_{admin.id}_{len(ids)}")
    if path is None:
        return json_result(False, tr(request, admin, "err_no_files_selected"), 404)
    await audit(session, request, admin, "articles_zip_download", f"{len(ids)} articles")
    return FileResponse(path, media_type="application/zip", filename="scholarbot_pdfs.zip",
                        background=BackgroundTask(lambda: path.unlink(missing_ok=True)))
