"""End-to-end processing of one request: resolve → find legal PDF → store → deliver → record."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Protocol

from app.core.runtime_settings import runtime
from app.database.database import session_scope
from app.database.models import Article, ArticleFile, Delivery, DownloadJob, ErrorEvent, User, utcnow
from app.database.repositories.articles import upsert_from_meta
from app.providers.base import ArticleMeta
from app.providers.registry import ProviderManager
from app.services.article_metadata import usable_links
from app.services.article_search import ArticleSearch
from app.services.library import Library
from app.services.parsing import InvalidInput, parse_input
from app.services.pdf_downloader import PDFDownloader

log = logging.getLogger(__name__)


@dataclass
class DeliveryReport:
    ok: bool
    message_id: int | None = None
    error: str | None = None


class Notifier(Protocol):
    async def progress(self, job: DownloadJob, stage: str) -> None: ...
    async def candidates(self, job: DownloadJob, metas: list[ArticleMeta]) -> None: ...
    async def article(self, job: DownloadJob, article: Article, file: ArticleFile | None, reason: str | None) -> DeliveryReport | None: ...
    async def failure(self, job: DownloadJob, code: str) -> None: ...


class NullNotifier:
    async def progress(self, job, stage): return None
    async def candidates(self, job, metas): return None
    async def article(self, job, article, file, reason): return None
    async def failure(self, job, code): return None


class ArticlePipeline:
    def __init__(self, providers: ProviderManager, search: ArticleSearch, downloader: PDFDownloader, library: Library,
                 notifier: Notifier | None = None):
        self.providers, self.search, self.downloader, self.library = providers, search, downloader, library
        self.notifier: Notifier = notifier or NullNotifier()
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock(self, key: str) -> asyncio.Lock:
        return self._locks.setdefault(key, asyncio.Lock())

    async def _update(self, job_id: int, **fields) -> None:
        async with session_scope() as s:
            job = await s.get(DownloadJob, job_id)
            for k, v in fields.items():
                setattr(job, k, v)

    async def run_job(self, job_id: int) -> None:
        async with session_scope() as s:
            job = await s.get(DownloadJob, job_id)
            if job is None or job.status not in ("queued", "processing"):
                return
            job.status, job.started_at = "processing", utcnow()
            await s.flush()
            s.expunge(job)
        await self.notifier.progress(job, "searching")
        try:
            if job.query_type == "pick":
                meta = ArticleMeta.from_dict((job.candidates or [{}])[0])
                res = await self.search.resolve_pick(meta)
            else:
                try:
                    parsed = parse_input(job.query_text, "author" if job.query_type == "author" else None)
                except InvalidInput as exc:
                    return await self._fail(job, exc.code)
                res = await self.search.resolve(parsed)
        except Exception:  # noqa: BLE001
            log.exception("resolve failed for job %s", job_id)
            return await self._fail(job, "internal", record=True)

        if res.kind == "error":
            return await self._fail(job, res.error_code or "sources_unavailable")
        if res.kind == "not_found":
            await self._update(job_id, status="not_found", finished_at=utcnow(), error_code="not_found")
            return await self.notifier.failure(job, "not_found")
        if res.kind == "candidates":
            await self._update(job_id, status="awaiting_choice", finished_at=utcnow(),
                               candidates=[m.to_dict() for m in res.candidates])
            return await self.notifier.candidates(job, res.candidates)
        assert res.meta is not None
        await self._process_article(job, res.meta)

    async def _fail(self, job: DownloadJob, code: str, record: bool = False) -> None:
        await self._update(job.id, status="failed", error_code=code, finished_at=utcnow())
        if record:
            async with session_scope() as s:
                s.add(ErrorEvent(kind="system", source="pipeline", message=f"job {job.id} failed: {code}"))
        await self.notifier.failure(job, code)

    async def _process_article(self, job: DownloadJob, meta: ArticleMeta) -> None:
        key = meta.doi or meta.title.lower()
        reason: str | None = None
        hit_source: str | None = None  # recorded after the write transaction below ends (SQLite allows one writer)
        async with self._lock(key):  # identical in-flight requests wait here and then hit the stored copy
            async with session_scope() as s:
                user = await s.get(User, job.user_id) if job.user_id else None
                article = await upsert_from_meta(s, meta, user)
                file = await self.library.cached_file(s, article)
                if file is None:
                    article.status = "processing"
                    links = usable_links(meta.links, runtime.get("require_explicit_license"))
                    links = self.providers.rank_links(links)
                    if not links:
                        reason = "no_legal_pdf"
                    else:
                        await self.notifier.progress(job, "downloading")
                        out = await self.downloader.download_first_valid(links, runtime.get("max_pdf_mb"))
                        if out.result:
                            file = await self.library.attach_file(s, article, out.result)
                            hit_source = out.result.link.source
                        else:
                            reason = {"too_large": "too_large", "corrupt": "file_corrupt", "truncated": "file_corrupt",
                                      "html_page": "only_publisher_page", "not_pdf": "only_publisher_page",
                                      "empty": "file_corrupt"}.get(out.reason, "download_failed")
                            if all(l.kind == "landing" for l in links) and reason == "download_failed":
                                reason = "only_publisher_page"
                    if file is None:
                        article.status = "failed"
                        article.access_status = article.access_status if article.access_status != "unknown" else "closed"
                await s.flush()
                article_id, file_id = article.id, file.id if file else None
        if hit_source:
            await self.providers.record_hit(hit_source)
        await self._update(job.id, article_id=article_id, source=(file.source if file else None),
                           status="found" if file is None else "found")
        async with session_scope() as s:
            article = await s.get(Article, article_id)
            file = await s.get(ArticleFile, file_id) if file_id else None
            job_fresh = await s.get(DownloadJob, job.id)
            report = await self.notifier.article(job_fresh, article, file, reason)
            if file is not None:
                s.add(Delivery(job_id=job.id, user_id=job.user_id, article_id=article.id, file_id=file.id,
                               status="sent" if report and report.ok else "failed",
                               bale_message_id=report.message_id if report else None,
                               error=(report.error if report and not report.ok else None), size_bytes=file.size_bytes))
                if report and report.ok:
                    article.status = "sent"
                elif report and not report.ok:
                    s.add(ErrorEvent(kind="delivery", source="bale", message=f"job {job.id}: {report.error}"[:1000]))
            job_row = await s.get(DownloadJob, job.id)
            job_row.finished_at = utcnow()
            if file is not None and report and report.ok:
                job_row.status = "sent"
            elif file is not None and report and not report.ok:
                job_row.status, job_row.error_code = "found", "delivery_failed"
            else:
                job_row.status, job_row.error_code = "found", reason
