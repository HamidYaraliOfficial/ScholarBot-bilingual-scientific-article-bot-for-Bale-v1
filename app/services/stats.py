"""Aggregations for the overview page and charts."""
from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import (Article, ArticleFile, Delivery, DownloadJob, ErrorEvent, SavedArticle, User, utcnow)

if TYPE_CHECKING:  # avoid a circular import (the container imports the bot, the bot admin panel imports this module)
    from app.services.container import Services


async def _count(s: AsyncSession, stmt) -> int:
    return (await s.execute(stmt)).scalar_one() or 0


async def overview(s: AsyncSession, svc: Services) -> dict:
    now = utcnow()
    usage = svc.storage.usage()
    jobs_total = await _count(s, select(func.count(DownloadJob.id)).where(DownloadJob.query_type != "pick"))
    jobs_ok = await _count(s, select(func.count(DownloadJob.id)).where(DownloadJob.status == "sent"))
    jobs_done = await _count(s, select(func.count(DownloadJob.id)).where(
        DownloadJob.status.in_(("sent", "found", "not_found", "failed"))))
    return {
        "users_total": await _count(s, select(func.count(User.id))),
        "users_active": await _count(s, select(func.count(User.id)).where(User.last_seen_at >= now - timedelta(days=7))),
        "searches": jobs_total,
        "articles_found": await _count(s, select(func.count(Article.id)).where(Article.status != "deleted")),
        "pdfs_downloaded": await _count(s, select(func.count(ArticleFile.id))),
        "sent": await _count(s, select(func.count(Delivery.id)).where(Delivery.status == "sent")),
        "saved": await _count(s, select(func.count(SavedArticle.id))),
        "deleted": await _count(s, select(func.count(Article.id)).where(Article.status == "deleted")),
        "stored_bytes": usage["stored_bytes"], "disk_used": usage["disk_used"], "disk_total": usage["disk_total"],
        "disk_free": usage["disk_free"],
        "errors_24h": await _count(s, select(func.count(ErrorEvent.id)).where(ErrorEvent.created_at >= now - timedelta(days=1))),
        "success_rate": round(100 * jobs_ok / jobs_done) if jobs_done else None,
        "bot": svc.runner.status, "queued": svc.tasks.queued, "active": svc.tasks.active,
    }


def _series(rows, days: int) -> tuple[list[str], dict[str, int]]:
    today = utcnow().date()
    labels = [(today - timedelta(days=days - 1 - i)).isoformat() for i in range(days)]
    return labels, {str(d)[:10]: n for d, n in rows}


async def charts(s: AsyncSession) -> dict:
    since = utcnow() - timedelta(days=30)
    day = func.date(DownloadJob.created_at)
    labels, req = _series((await s.execute(select(day, func.count()).where(DownloadJob.created_at >= since,
                                                                           DownloadJob.query_type != "pick").group_by(day))).all(), 30)
    _, got = _series((await s.execute(select(day, func.count()).where(DownloadJob.created_at >= since,
                                                                      DownloadJob.status == "sent").group_by(day))).all(), 30)
    _, done = _series((await s.execute(select(day, func.count()).where(
        DownloadJob.created_at >= since, DownloadJob.status.in_(("sent", "found", "not_found", "failed"))).group_by(day))).all(), 30)
    rate = [round(100 * got.get(d, 0) / done[d]) if done.get(d) else None for d in labels]

    weeks, active = [], []
    now = utcnow()
    for i in range(7, -1, -1):
        start, end = now - timedelta(days=7 * (i + 1)), now - timedelta(days=7 * i)
        weeks.append(end.date().isoformat())
        active.append(await _count(s, select(func.count(func.distinct(DownloadJob.user_id))).where(
            DownloadJob.created_at >= start, DownloadJob.created_at < end)))
    sources = (await s.execute(select(Article.source, func.count()).where(Article.source.is_not(None)).group_by(Article.source)
                               .order_by(func.count().desc()).limit(8))).all()
    return {"days": labels, "requests": [req.get(d, 0) for d in labels], "received": [got.get(d, 0) for d in labels],
            "success_rate": rate, "weeks": weeks, "active_users": active,
            "sources": {"labels": [r[0] for r in sources], "values": [r[1] for r in sources]}}
