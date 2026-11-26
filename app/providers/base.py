"""Provider abstractions, shared data types and a polite, retrying HTTP wrapper."""
from __future__ import annotations

import asyncio
import html
import re
import time
from dataclasses import asdict, dataclass, field
from typing import Any
from urllib.parse import unquote

import httpx

from app.core.config import Settings, get_settings
from app.core.runtime_settings import runtime
from app.core.safe_http import USER_AGENT

DOI_RE = re.compile(r"10\.\d{4,9}/[^\s\"'<>]+", re.I)
ARXIV_NEW_RE = re.compile(r"(?<![\d.])(\d{4}\.\d{4,5})(v\d+)?(?![\d])")
ARXIV_OLD_RE = re.compile(r"([a-z\-]+(?:\.[A-Z]{2})?/\d{7})(v\d+)?", re.I)
VERSION_RANK = {"publishedversion": 3, "acceptedversion": 2, "submittedversion": 1}


def normalize_doi(value: str | None) -> str | None:
    if not value:
        return None
    v = unquote(value.strip())
    m = DOI_RE.search(v)
    if not m:
        return None
    doi = m.group(0).rstrip(".,;:)]}>\"'").lower()
    return doi if len(doi) > 7 else None


def strip_tags(text: str | None) -> str | None:
    if not text:
        return None
    text = re.sub(r"</?(?:\w+:)?(?:p|br|title|sec)\b[^>]*>", " ", text)  # block-level tags separate words
    text = html.unescape(re.sub(r"<[^>]+>", "", text))
    return re.sub(r"\s+", " ", text).strip() or None


class ProviderError(Exception):
    """kind: timeout | unavailable | rate_limited | auth | bad_response"""

    def __init__(self, provider: str, kind: str, message: str = ""):
        super().__init__(f"{provider}: {kind} {message}".strip())
        self.provider, self.kind, self.message = provider, kind, message


@dataclass
class OALink:
    url: str
    source: str
    kind: str = "pdf"  # pdf | landing
    version: str | None = None
    license: str | None = None
    host_type: str | None = None  # publisher | repository

    @property
    def score(self) -> tuple:
        rank = VERSION_RANK.get((self.version or "").lower(), 1.5)
        pref = {"europe_pmc": 3, "arxiv": 3, "unpaywall": 2, "openalex": 2, "doaj": 2, "core": 1}.get(self.source, 1)
        return (self.kind == "pdf", rank, bool(self.license), pref)


@dataclass
class Ref:
    """Identifiers we can use to look an article up."""
    doi: str | None = None
    arxiv_id: str | None = None
    pmid: str | None = None
    pmcid: str | None = None
    openalex_id: str | None = None


@dataclass
class ArticleMeta:
    title: str = ""
    original_title: str | None = None
    authors: list[str] = field(default_factory=list)
    year: int | None = None
    journal: str | None = None
    volume: str | None = None
    issue: str | None = None
    pages: str | None = None
    issn: str | None = None
    publisher: str | None = None
    abstract: str | None = None
    keywords: list[str] = field(default_factory=list)
    work_type: str | None = None
    doi: str | None = None
    landing_url: str | None = None
    access_status: str = "unknown"  # open | closed | unknown
    license: str | None = None
    arxiv_id: str | None = None
    pmid: str | None = None
    pmcid: str | None = None
    openalex_id: str | None = None
    sources: list[str] = field(default_factory=list)
    links: list[OALink] = field(default_factory=list)

    @property
    def doi_url(self) -> str | None:
        return f"https://doi.org/{self.doi}" if self.doi else None

    def ref(self) -> Ref:
        return Ref(self.doi, self.arxiv_id, self.pmid, self.pmcid, self.openalex_id)

    def merge(self, other: "ArticleMeta") -> "ArticleMeta":
        """Fill gaps from `other` (self wins on conflicts)."""
        for name in ("title", "original_title", "year", "journal", "volume", "issue", "pages", "issn", "publisher",
                     "abstract", "work_type", "doi", "landing_url", "license", "arxiv_id", "pmid", "pmcid",
                     "openalex_id"):
            if not getattr(self, name) and getattr(other, name):
                setattr(self, name, getattr(other, name))
        if not self.authors:
            self.authors = list(other.authors)
        if not self.keywords:
            self.keywords = list(other.keywords)
        if other.access_status == "open" or self.access_status == "unknown":
            self.access_status = other.access_status if other.access_status != "unknown" else self.access_status
        self.sources = list(dict.fromkeys(self.sources + other.sources))
        seen = {_url_key(l.url) for l in self.links}
        for link in other.links:
            if _url_key(link.url) not in seen:
                self.links.append(link)
                seen.add(_url_key(link.url))
        return self

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ArticleMeta":
        d = dict(d)
        d["links"] = [OALink(**l) for l in d.get("links", [])]
        d.pop("doi_url", None)
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


def _url_key(url: str) -> str:
    return re.sub(r"^https?://(www\.)?", "", url.strip().lower()).rstrip("/")


class ProviderHTTP:
    """One shared HTTP client: timeouts, bounded retries with backoff, per-provider concurrency & pacing."""

    def __init__(self, name: str, *, settings: Settings | None = None, transport: httpx.AsyncBaseTransport | None = None,
                 max_concurrency: int = 3, min_interval: float = 0.0, headers: dict | None = None):
        self.name = name
        self.settings = settings or get_settings()
        ua = USER_AGENT + (f" mailto:{self.settings.contact_email}" if self.settings.contact_email else "")
        self._client = httpx.AsyncClient(transport=transport, follow_redirects=False,
                                         headers={"User-Agent": ua, "Accept": "application/json", **(headers or {})})
        self._sem = asyncio.Semaphore(max_concurrency)
        self._min_interval, self._last = min_interval, 0.0
        self._pace_lock = asyncio.Lock()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _pace(self) -> None:
        if not self._min_interval:
            return
        async with self._pace_lock:
            wait = self._last + self._min_interval - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last = time.monotonic()

    async def get(self, url: str, params: dict | None = None, headers: dict | None = None,
                  allow_404: bool = False) -> httpx.Response | None:
        timeout = runtime.get("provider_timeout_seconds")
        retries = runtime.get("provider_retries")
        last_exc: ProviderError | None = None
        for attempt in range(retries + 1):
            try:
                async with self._sem:
                    await self._pace()
                    resp = await self._client.get(url, params=params, headers=headers, timeout=timeout)
            except httpx.TimeoutException:
                last_exc = ProviderError(self.name, "timeout")
            except httpx.HTTPError as exc:
                last_exc = ProviderError(self.name, "unavailable", type(exc).__name__)
            else:
                code = resp.status_code
                if code == 404 and allow_404:
                    return None
                if code < 400:
                    return resp
                if code in (401, 403):
                    raise ProviderError(self.name, "auth", f"HTTP {code}")
                if code == 429:
                    last_exc = ProviderError(self.name, "rate_limited", "HTTP 429")
                    delay = min(float(resp.headers.get("retry-after", 2) or 2), 10)
                    if attempt < retries:
                        await asyncio.sleep(delay)
                    continue
                if code >= 500:
                    last_exc = ProviderError(self.name, "unavailable", f"HTTP {code}")
                else:
                    raise ProviderError(self.name, "bad_response", f"HTTP {code}")
            if attempt < retries:
                await asyncio.sleep(min(0.5 * 2 ** attempt, 4))
        assert last_exc is not None
        raise last_exc

    async def get_json(self, url: str, params: dict | None = None, headers: dict | None = None,
                       allow_404: bool = False) -> Any | None:
        resp = await self.get(url, params, headers, allow_404)
        if resp is None:
            return None
        try:
            return resp.json()
        except ValueError as exc:
            raise ProviderError(self.name, "bad_response", "invalid JSON") from exc


class Provider:
    """Base class. Subclasses implement whichever of lookup/search they support."""
    name = "base"
    supports_search = False
    supports_lookup = True
    key_env: str | None = None  # name of the env var holding an optional/required key (for the dashboard)
    key_required = False
    description = ""

    def __init__(self, http: ProviderHTTP, settings: Settings | None = None):
        self.http = http
        self.settings = settings or get_settings()

    def is_configured(self) -> bool:
        return True

    def supports(self, ref: Ref) -> bool:
        """Can this provider look the article up with the identifiers we have? (cheap, no I/O)"""
        return bool(ref.doi)

    def config_hint(self) -> str:
        return ""

    async def lookup(self, ref: Ref) -> ArticleMeta | None:  # pragma: no cover - interface
        return None

    async def search(self, query: str, kind: str, limit: int) -> list[ArticleMeta]:  # pragma: no cover
        return []

    async def health_check(self) -> None:  # pragma: no cover - interface
        raise NotImplementedError
