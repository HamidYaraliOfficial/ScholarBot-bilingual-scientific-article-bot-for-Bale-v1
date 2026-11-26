"""DOAJ — Directory of Open Access Journals (every indexed article is open access)."""
from __future__ import annotations

from urllib.parse import quote

from .base import ArticleMeta, OALink, Provider, Ref, normalize_doi, strip_tags

BASE = "https://doaj.org/api/search/articles"


def parse_hit(hit: dict) -> ArticleMeta:
    b = hit.get("bibjson") or {}
    journal = b.get("journal") or {}
    ids = {i.get("type"): i.get("id") for i in b.get("identifier") or []}
    lic = (journal.get("license") or [{}])[0].get("type")
    pages = "-".join(x for x in (b.get("start_page"), b.get("end_page")) if x) or None
    links = []
    for l in b.get("link") or []:
        if l.get("type") == "fulltext" and l.get("url"):
            is_pdf = (l.get("content_type") or "").upper() == "PDF" or l["url"].lower().split("?")[0].endswith(".pdf")
            links.append(OALink(l["url"], "doaj", "pdf" if is_pdf else "landing", "publishedVersion", lic, "publisher"))
    year = b.get("year")
    return ArticleMeta(
        title=strip_tags(b.get("title")) or "", authors=[a["name"] for a in b.get("author") or [] if a.get("name")],
        year=int(year) if str(year).isdigit() else None, journal=journal.get("title"), volume=journal.get("volume"),
        issue=journal.get("number"), pages=pages, issn=ids.get("eissn") or ids.get("pissn") or (journal.get("issns") or [None])[0],
        publisher=journal.get("publisher"), abstract=strip_tags(b.get("abstract")), keywords=b.get("keywords") or [],
        work_type="journal-article", doi=normalize_doi(ids.get("doi")), access_status="open", license=lic,
        landing_url=(links[0].url if links else None), sources=["doaj"], links=links)


class DOAJ(Provider):
    name = "doaj"
    supports_search = True
    description = "DOAJ — fully open-access journals"

    async def _q(self, query: str, limit: int) -> list[ArticleMeta]:
        data = await self.http.get_json(f"{BASE}/{quote(query, safe='')}", {"pageSize": limit}, allow_404=True)
        return [parse_hit(h) for h in (data or {}).get("results", [])]

    async def lookup(self, ref: Ref) -> ArticleMeta | None:
        if not ref.doi:
            return None
        res = await self._q(f'doi:"{ref.doi}"', 1)
        return res[0] if res else None

    async def search(self, query: str, kind: str, limit: int) -> list[ArticleMeta]:
        clean = query.replace('"', " ")
        return await self._q(f'bibjson.author.name:"{clean}"' if kind == "author" else f'bibjson.title:"{clean}"', limit)

    async def health_check(self) -> None:
        await self._q("bibjson.title:climate", 1)
