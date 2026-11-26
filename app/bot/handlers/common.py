"""Shared bot context and text builders."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.bot.bale_client import BaleClient, BaleError, with_retry
from app.core.localization import t
from app.database.models import Article, ArticleFile
from app.services.library import Library
from app.services.storage_manager import StorageManager
from app.services.task_manager import TaskManager

SOURCE_NAMES = {"crossref": "Crossref", "openalex": "OpenAlex", "unpaywall": "Unpaywall", "europe_pmc": "Europe PMC",
                "doaj": "DOAJ", "arxiv": "arXiv", "core": "CORE"}


@dataclass
class BotContext:
    client: BaleClient
    tasks: TaskManager
    library: Library
    storage: StorageManager
    runner: Any = None  # BotRunner: gives the admin panel access to providers, maintenance and settings


async def reply(ctx: BotContext, chat_id: int, text: str, markup: dict | None = None) -> dict | None:
    try:
        return await with_retry(lambda: ctx.client.send_message(chat_id, text, markup))
    except BaleError:
        return None


def fmt_size(n: int | None) -> str:
    if not n:
        return ""
    return f"{n / 1024 / 1024:.1f} MB" if n >= 1024 * 1024 else f"{n / 1024:.0f} KB"


def authors_text(lang: str, authors: list[str], limit: int = 6) -> str:
    if not authors:
        return ""
    shown = ", ".join(authors[:limit])
    return shown + (f" {t(lang, 'et_al')}" if len(authors) > limit else "")


def article_info(lang: str, a: Article, f: ArticleFile | None, header_key: str = "msg_article_found",
                 footer: str | None = None) -> str:
    """Info block shaped like the examples in the project brief; missing fields are simply left out."""
    access = {"open": "access_open", "closed": "access_closed"}.get(a.access_status, "access_unknown")
    fields = [("lbl_title", a.title), ("lbl_authors", authors_text(lang, a.authors or [])), ("lbl_year", a.year),
              ("lbl_journal", a.journal), ("lbl_doi", a.doi), ("lbl_access", t(lang, access)),
              ("lbl_source", SOURCE_NAMES.get(a.source or "", a.source or "") if f else ""),
              ("lbl_size", fmt_size(f.size_bytes) if f else "")]
    parts = [t(lang, header_key)] + [f"{t(lang, k)}: {v}" for k, v in fields if v]
    if footer:
        parts.append(footer)
    return "\n\n".join(parts)


def official_links(a: Article) -> str:
    return "\n".join(x for x in (a.doi_url, a.landing_url) if x)


def meta_from_article(a: Article) -> dict[str, Any]:
    return {"title": a.title, "authors": a.authors or [], "year": a.year, "journal": a.journal, "doi": a.doi,
            "volume": a.volume, "issue": a.issue, "pages": a.pages, "publisher": a.publisher, "abstract": a.abstract,
            "keywords": a.keywords or [], "work_type": a.work_type, "landing_url": a.landing_url,
            "access_status": a.access_status, "license": a.license, "sources": [], "links": []}
