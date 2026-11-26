"""Crossref REST API — authoritative bibliographic metadata (no PDFs)."""
from __future__ import annotations

from urllib.parse import quote

from .base import ArticleMeta, Provider, Ref, strip_tags

BASE = "https://api.crossref.org"


def _year(item: dict) -> int | None:
    for key in ("issued", "published-print", "published-online", "published", "created"):
        parts = (item.get(key) or {}).get("date-parts") or [[None]]
        if parts and parts[0] and parts[0][0]:
            return int(parts[0][0])
    return None


def parse_item(item: dict) -> ArticleMeta:
    authors = []
    for a in item.get("author") or []:
        name = " ".join(x for x in (a.get("given"), a.get("family")) if x) or a.get("name")
        if name:
            authors.append(name)
    lic = next((l.get("URL") for l in item.get("license") or [] if l.get("content-version") == "vor"), None)
    first = lambda k: (item.get(k) or [None])[0]  # noqa: E731
    return ArticleMeta(
        title=strip_tags(first("title")) or "", original_title=strip_tags(first("original-title")), authors=authors,
        year=_year(item), journal=strip_tags(first("container-title")), volume=item.get("volume"),
        issue=item.get("issue"), pages=item.get("page"), issn=first("ISSN"), publisher=item.get("publisher"),
        abstract=strip_tags(item.get("abstract")), keywords=item.get("subject") or [], work_type=item.get("type"),
        doi=(item.get("DOI") or "").lower() or None, landing_url=(item.get("resource") or {}).get("primary", {}).get("URL")
        or item.get("URL"), license=lic, sources=["crossref"])


class Crossref(Provider):
    name = "crossref"
    supports_search = True
    description = "Crossref REST API — bibliographic metadata"

    def _params(self, **kw) -> dict:
        if self.settings.contact_email:
            kw["mailto"] = self.settings.contact_email
        return kw

    async def lookup(self, ref: Ref) -> ArticleMeta | None:
        if not ref.doi:
            return None
        data = await self.http.get_json(f"{BASE}/works/{quote(ref.doi, safe='/()')}", self._params(), allow_404=True)
        return parse_item(data["message"]) if data and data.get("message") else None

    async def search(self, query: str, kind: str, limit: int) -> list[ArticleMeta]:
        field = "query.author" if kind == "author" else "query.bibliographic"
        data = await self.http.get_json(f"{BASE}/works", self._params(**{field: query, "rows": limit}))
        return [parse_item(i) for i in (data or {}).get("message", {}).get("items", []) if i.get("DOI")]

    async def health_check(self) -> None:
        await self.http.get_json(f"{BASE}/works", self._params(rows=1, select="DOI"))
