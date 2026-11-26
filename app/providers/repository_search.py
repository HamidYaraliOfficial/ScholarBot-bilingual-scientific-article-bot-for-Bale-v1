"""CORE — aggregator of institutional / subject repositories (needs a free API key)."""
from __future__ import annotations

from .base import ArticleMeta, OALink, Provider, Ref, normalize_doi, strip_tags

BASE = "https://api.core.ac.uk/v3/search/works"


def parse_work(w: dict) -> ArticleMeta:
    links = []
    if w.get("downloadUrl"):
        links.append(OALink(w["downloadUrl"], "core", "pdf", None, None, "repository"))
    journals = w.get("journals") or []
    return ArticleMeta(
        title=strip_tags(w.get("title")) or "", authors=[a.get("name") for a in w.get("authors") or [] if a.get("name")],
        year=w.get("yearPublished"), journal=(journals[0].get("title") if journals else None),
        publisher=w.get("publisher"), abstract=strip_tags(w.get("abstract")), doi=normalize_doi(w.get("doi")),
        work_type=w.get("documentType"), access_status="open" if links else "unknown", sources=["core"], links=links,
        landing_url=next((l.get("url") for l in w.get("links") or [] if l.get("type") == "display"), None))


class CoreRepositories(Provider):
    name = "core"
    supports_search = True
    key_env = "CORE_API_KEY"
    key_required = True
    description = "CORE — institutional and subject repositories"

    def is_configured(self) -> bool:
        return bool(self.settings.core_api_key)

    def config_hint(self) -> str:
        return "" if self.is_configured() else "Set CORE_API_KEY in .env (free key from core.ac.uk/services/api)"

    async def _q(self, q: str, limit: int) -> list[ArticleMeta]:
        data = await self.http.get_json(BASE, {"q": q, "limit": limit},
                                        headers={"Authorization": f"Bearer {self.settings.core_api_key}"})
        return [parse_work(w) for w in (data or {}).get("results", [])]

    async def lookup(self, ref: Ref) -> ArticleMeta | None:
        if not ref.doi:
            return None
        res = await self._q(f'doi:"{ref.doi}"', 1)
        return res[0] if res else None

    async def search(self, query: str, kind: str, limit: int) -> list[ArticleMeta]:
        clean = query.replace('"', " ")
        return await self._q(f'authors:"{clean}"' if kind == "author" else f'title:"{clean}"', limit)

    async def health_check(self) -> None:
        if self.is_configured():
            await self._q('title:"open access"', 1)
