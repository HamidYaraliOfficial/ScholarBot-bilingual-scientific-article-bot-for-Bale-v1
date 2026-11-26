"""Turns user input into bibliographic metadata (+ legal open-access links) using the providers."""
from __future__ import annotations

import difflib
import logging
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx

from app.core.config import Settings, get_settings
from app.core.runtime_settings import runtime
from app.core.safe_http import UnsafeURLError, fetch_limited
from app.providers.base import ArticleMeta, ProviderError, Ref, normalize_doi
from app.providers.registry import ProviderManager
from app.services.article_metadata import normalize_title
from app.services.parsing import ParsedQuery, doi_in_url
from app.services.pdf_downloader import parse_landing_html

log = logging.getLogger(__name__)
# Sources we never visit. DOIs found inside such URLs are still resolved through legal providers only.
BLOCKED_HOST_PARTS = ("sci-hub", "scihub", "libgen", "z-lib", "zlibrary", "annas-archive", "sci-net", "scholar.archive")
AUTO_PICK_SIMILARITY = 0.93


@dataclass
class Resolution:
    kind: str  # article | candidates | not_found | error
    meta: ArticleMeta | None = None
    candidates: list[ArticleMeta] = field(default_factory=list)
    errors: list[ProviderError] = field(default_factory=list)
    error_code: str | None = None


class ArticleSearch:
    def __init__(self, providers: ProviderManager, settings: Settings | None = None, transport: httpx.AsyncBaseTransport | None = None):
        self.providers, self.s, self.transport = providers, settings or get_settings(), transport

    async def resolve(self, q: ParsedQuery) -> Resolution:
        if q.kind in ("doi", "arxiv", "pmid", "pmcid"):
            return await self._by_ref(q.ref or Ref())
        if q.kind == "url":
            return await self._by_url(q.value)
        return await self._by_search(q)

    async def resolve_pick(self, meta: ArticleMeta) -> Resolution:
        """The user chose a candidate: enrich it with links from every provider."""
        res = await self._by_ref(meta.ref())
        if res.meta is None:
            return Resolution("article", meta, errors=res.errors)
        return Resolution("article", meta.merge(res.meta), errors=res.errors)

    async def _by_ref(self, ref: Ref) -> Resolution:
        lr = await self.providers.lookup(ref)
        if lr.meta:
            return Resolution("article", lr.meta, errors=lr.errors)
        if lr.errors and len({e.provider for e in lr.errors}) >= lr.attempted:
            return Resolution("error", errors=lr.errors, error_code="sources_unavailable")
        return Resolution("not_found", errors=lr.errors)

    async def _by_search(self, q: ParsedQuery) -> Resolution:
        limit = runtime.get("search_results_limit")
        metas, errors = await self.providers.search(q.value, q.kind, limit)
        if not metas:
            if errors and len(errors) >= len(self.providers.active(need_search=True)):
                return Resolution("error", errors=errors, error_code="sources_unavailable")
            return Resolution("not_found", errors=errors)
        if q.kind == "title":
            top = metas[0]
            sim = difflib.SequenceMatcher(None, normalize_title(q.value), normalize_title(top.title)).ratio()
            second = (difflib.SequenceMatcher(None, normalize_title(q.value), normalize_title(metas[1].title)).ratio()
                      if len(metas) > 1 else 0)
            if sim >= AUTO_PICK_SIMILARITY and second < AUTO_PICK_SIMILARITY:
                return await self.resolve_pick(top)
        return Resolution("candidates", candidates=metas, errors=errors)

    async def _by_url(self, url: str) -> Resolution:
        if doi := doi_in_url(url):
            return await self._by_ref(Ref(doi=doi))
        host = (urlsplit(url).hostname or "").lower()
        if any(part in host for part in BLOCKED_HOST_PARTS):
            return Resolution("error", error_code="unsupported_source")
        try:
            body, ctype, final = await fetch_limited(url, 1_500_000, timeout=15, allow_private=self.s.allow_private_urls,
                                                     transport=self.transport, headers={"Accept": "text/html,*/*"})
        except UnsafeURLError:
            return Resolution("error", error_code="unsafe_url")
        except httpx.HTTPError:
            return Resolution("error", error_code="url_unreadable")
        if "html" not in ctype.lower() and not body.lstrip().startswith(b"<"):
            return Resolution("error", error_code="url_unreadable")
        page = parse_landing_html(body.decode("utf-8", "ignore"))
        doi = None
        for key in ("citation_doi", "prism.doi", "dc.identifier", "dc.identifier.doi", "bepress_citation_doi"):
            for val in page.meta.get(key, []):
                doi = doi or normalize_doi(val)
        if doi:
            return await self._by_ref(Ref(doi=doi))
        if aid := (page.meta.get("citation_arxiv_id") or [None])[0]:
            return await self._by_ref(Ref(arxiv_id=aid))
        if title := (page.meta.get("citation_title") or page.meta.get("dc.title") or [None])[0]:
            return await self._by_search(ParsedQuery("title", title))
        return Resolution("error", error_code="url_unreadable")
