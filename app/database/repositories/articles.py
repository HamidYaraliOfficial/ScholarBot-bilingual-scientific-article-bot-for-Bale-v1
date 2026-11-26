from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import String, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import Article, ArticleFile, SavedArticle, User, utcnow
from app.providers.base import ArticleMeta
from app.services.article_metadata import title_hash

SORTABLE = {"created_at": Article.created_at, "title": Article.title, "year": Article.year, "status": Article.status,
            "last_requested_at": Article.last_requested_at, "size": ArticleFile.size_bytes, "doi": Article.doi}


async def find_existing(session: AsyncSession, meta: ArticleMeta) -> Article | None:
    if meta.doi:
        found = (await session.execute(select(Article).where(Article.doi == meta.doi))).scalar_one_or_none()
        if found:
            return found
    th = title_hash(meta.title, meta.authors, meta.year)
    if th:
        stmt = select(Article).where(Article.title_hash == th, or_(Article.doi.is_(None), Article.doi == meta.doi))
        return (await session.execute(stmt)).scalars().first()
    return None


async def upsert_from_meta(session: AsyncSession, meta: ArticleMeta, user: User | None) -> Article:
    art = await find_existing(session, meta)
    th = title_hash(meta.title, meta.authors, meta.year)
    if art is None:
        # initialise relationships explicitly so no lazy load (forbidden in async code) is ever triggered
        art = Article(doi=meta.doi, title_hash=th, request_count=0, file=None, saved=None, last_user=None)
        session.add(art)
    for name in ("title", "original_title", "year", "journal", "volume", "issue", "pages", "issn", "publisher",
                 "abstract", "work_type", "license", "landing_url"):
        val = getattr(meta, name)
        if val and not getattr(art, name, None):
            setattr(art, name, val)
    if not art.doi and meta.doi:
        art.doi = meta.doi
    art.doi_url = f"https://doi.org/{art.doi}" if art.doi else None
    art.authors = art.authors or meta.authors
    art.keywords = art.keywords or meta.keywords
    art.title_hash = art.title_hash or th
    if meta.access_status == "open" or art.access_status == "unknown":
        art.access_status = meta.access_status if meta.access_status != "unknown" else art.access_status
    art.metadata_sources = list(dict.fromkeys((art.metadata_sources or []) + meta.sources))
    art.request_count = (art.request_count or 0) + 1
    art.last_requested_at = utcnow()
    if user is not None:
        art.last_user = user
    if art.status == "deleted":
        art.status = "processing"
    await session.flush()
    return art


@dataclass
class ArticleFilters:
    q: str = ""
    doi: str = ""
    author: str = ""
    year: int | None = None
    source: str = ""
    status: str = ""
    saved: str = ""  # "" | saved | temporary
    user_id: int | None = None

    def as_query(self) -> dict[str, str]:
        return {k: str(v) for k, v in self.__dict__.items() if v not in ("", None)}


def apply_filters(stmt, f: ArticleFilters):
    if f.status == "deleted":
        stmt = stmt.where(Article.status == "deleted")
    elif f.status:
        stmt = stmt.where(Article.status == f.status)
    else:
        stmt = stmt.where(Article.status != "deleted")
    if f.q:
        like = f"%{f.q.lower()}%"
        stmt = stmt.where(or_(func.lower(Article.title).like(like), func.lower(Article.doi).like(like)))
    if f.doi:
        stmt = stmt.where(func.lower(Article.doi).like(f"%{f.doi.lower()}%"))
    if f.author:
        stmt = stmt.where(func.lower(cast(Article.authors, String)).like(f"%{f.author.lower()}%"))
    if f.year:
        stmt = stmt.where(Article.year == f.year)
    if f.source:
        stmt = stmt.where(Article.source == f.source)
    if f.user_id:
        stmt = stmt.where(Article.last_user_id == f.user_id)
    if f.saved == "saved":
        stmt = stmt.where(Article.id.in_(select(SavedArticle.article_id)))
    elif f.saved == "temporary":
        stmt = stmt.where(Article.id.not_in(select(SavedArticle.article_id)), Article.file_id.is_not(None))
    return stmt


async def query_articles(session: AsyncSession, f: ArticleFilters, *, sort: str = "created_at", desc: bool = True,
                         page: int = 1, per_page: int = 25) -> tuple[list[Article], int]:
    base = apply_filters(select(Article), f)
    total = (await session.execute(apply_filters(select(func.count(Article.id)), f))).scalar_one()
    col = SORTABLE.get(sort, Article.created_at)
    if col is ArticleFile.size_bytes:
        base = base.outerjoin(ArticleFile, Article.file_id == ArticleFile.id)
    base = base.order_by(col.desc() if desc else col.asc(), Article.id.desc())
    rows = (await session.execute(base.limit(per_page).offset((page - 1) * per_page))).unique().scalars().all()
    return list(rows), total


async def ids_for_filters(session: AsyncSession, f: ArticleFilters, limit: int = 5000) -> list[int]:
    stmt = apply_filters(select(Article.id), f).limit(limit)
    return [r for (r,) in (await session.execute(stmt)).all()]


async def get_many(session: AsyncSession, ids: list[int]) -> list[Article]:
    if not ids:
        return []
    return list((await session.execute(select(Article).where(Article.id.in_(ids)))).unique().scalars().all())
