"""Article library operations: attaching files, permanent save/unsave, deletion, cleanup, zip export."""
from __future__ import annotations

import zipfile
from datetime import timedelta
from pathlib import Path

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.compat import run_blocking
from app.core.security import safe_filename
from app.database.models import Article, ArticleFile, SavedArticle, utcnow
from app.services.pdf_downloader import DownloadResult
from app.services.storage_manager import StorageManager


class Library:
    def __init__(self, storage: StorageManager):
        self.storage = storage

    # --- files ------------------------------------------------------------------------------
    async def cached_file(self, session: AsyncSession, article: Article) -> ArticleFile | None:
        """Reusable stored PDF for this article (None if missing on disk — the stale row is cleared)."""
        f = article.file
        if f is None:
            return None
        if self.storage.exists(f.path, f.is_permanent):
            f.last_used_at = utcnow()
            return f
        article.file_id = None
        article.file = None
        return None

    async def attach_file(self, session: AsyncSession, article: Article, dl: DownloadResult) -> ArticleFile:
        existing = (await session.execute(select(ArticleFile).where(ArticleFile.sha256 == dl.sha256))).scalar_one_or_none()
        if existing and self.storage.exists(existing.path, existing.is_permanent):
            dl.part_path.unlink(missing_ok=True)  # type: ignore[union-attr]
            f = existing
        else:
            rel = self.storage.adopt(dl.part_path, dl.sha256, article.title or article.doi or "article", permanent=False)  # type: ignore[arg-type]
            if existing:
                f = existing
                f.path, f.is_permanent, f.size_bytes, f.page_count = rel, False, dl.size, dl.pages
            else:
                f = ArticleFile(sha256=dl.sha256, path=rel, size_bytes=dl.size, page_count=dl.pages, validation_status="valid",
                                source=dl.link.source, license=dl.link.license, is_permanent=False)
                session.add(f)
        f.last_used_at = utcnow()
        await session.flush()
        article.file_id = f.id
        article.file = f
        article.pdf_url = dl.final_url[:1024]
        article.source = dl.link.source
        article.license = article.license or dl.link.license
        article.access_status = "open"
        article.status = "received"
        return f

    async def _refs(self, session: AsyncSession, file_id: int, exclude: set[int] | None = None) -> int:
        stmt = select(func.count(Article.id)).where(Article.file_id == file_id, Article.status != "deleted")
        if exclude:
            stmt = stmt.where(Article.id.not_in(exclude))
        return (await session.execute(stmt)).scalar_one()

    async def _saved_refs(self, session: AsyncSession, file_id: int, exclude: set[int]) -> int:
        stmt = select(func.count(SavedArticle.id)).join(Article, Article.id == SavedArticle.article_id).where(
            Article.file_id == file_id, Article.id.not_in(exclude))
        return (await session.execute(stmt)).scalar_one()

    # --- permanent library --------------------------------------------------------------------
    async def save(self, session: AsyncSession, articles: list[Article], admin: str) -> dict[str, int]:
        res = {"saved": 0, "already": 0, "no_file": 0}
        for a in articles:
            if a.status == "deleted":
                continue
            if a.saved is not None:
                res["already"] += 1
                continue
            f = a.file
            if f is not None and not f.is_permanent:
                if self.storage.move(f.path, False, True):
                    f.is_permanent = True
                else:
                    a.file_id, a.file = None, None
                    f = None
            if f is None:
                res["no_file"] += 1
            a.saved = SavedArticle(article_id=a.id, saved_by=admin)
            res["saved"] += 1
        await session.flush()
        return res

    async def unsave(self, session: AsyncSession, articles: list[Article]) -> int:
        ids = {a.id for a in articles}
        n = 0
        for a in articles:
            if a.saved is None:
                continue
            if a.file is not None and a.file.is_permanent and not await self._saved_refs(session, a.file.id, ids):
                if self.storage.move(a.file.path, True, False):
                    a.file.is_permanent = False
                    a.file.last_used_at = utcnow()
            a.saved = None  # delete-orphan cascade removes the SavedArticle row
            n += 1
        await session.flush()
        return n

    async def delete_articles(self, session: AsyncSession, articles: list[Article], mode: str) -> dict[str, int]:
        """mode 'info': mark the records deleted, keep files (they fall back to temporary retention).
        mode 'info_and_file': also remove stored PDFs that no other live article depends on."""
        res = {"deleted": 0, "files_removed": 0, "shared_files_kept": 0}
        ids = {a.id for a in articles}
        await self.unsave(session, articles)
        for a in articles:
            f = a.file
            if mode == "info_and_file" and f is not None:
                if await self._refs(session, f.id, exclude=ids) == 0:
                    self.storage.delete(f.path, f.is_permanent)
                    await session.execute(delete(ArticleFile).where(ArticleFile.id == f.id))
                    res["files_removed"] += 1
                    a.file, a.file_id = None, None
                else:
                    res["shared_files_kept"] += 1
            a.status = "deleted"
            res["deleted"] += 1
        await session.flush()
        return res

    # --- maintenance ----------------------------------------------------------------------------
    async def cleanup(self, session: AsyncSession, retention_hours: int) -> dict[str, int]:
        cutoff = utcnow() - timedelta(hours=retention_hours)
        rows = (await session.execute(select(ArticleFile).where(ArticleFile.is_permanent.is_(False),
                                                                ArticleFile.last_used_at < cutoff))).scalars().all()
        removed = 0
        for f in rows:
            self.storage.delete(f.path, False)
            await session.execute(delete(ArticleFile).where(ArticleFile.id == f.id))
            removed += 1
        scratch = await run_blocking(self.storage.purge_scratch)
        return {"temp_files_removed": removed, "scratch_removed": scratch}

    async def zip_files(self, articles: list[Article], name: str) -> Path | None:
        items = [(a, a.file) for a in articles if a.file is not None and self.storage.exists(a.file.path, a.file.is_permanent)]
        if not items:
            return None
        out = self.storage.s.export_dir / f"{safe_filename(name)}.zip"

        def build() -> None:
            with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED) as z:
                used: set[str] = set()
                for a, f in items:
                    arc = f"{safe_filename(a.doi or a.title, 70)}.pdf"
                    while arc in used:
                        arc = f"{a.id}_{arc}"
                    used.add(arc)
                    z.write(self.storage.abs_path(f.path, f.is_permanent), arc)

        await run_blocking(build)
        return out
