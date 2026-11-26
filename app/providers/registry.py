"""Provider registry/manager: enable-disable, priority, health, stats and parallel look-ups."""
from __future__ import annotations

import asyncio
import difflib
import json
import logging
import time
from dataclasses import dataclass, field

import httpx
from sqlalchemy import select

from app.core.config import Settings, get_settings
from app.database.database import session_scope
from app.database.models import ErrorEvent, ProviderConfig, utcnow
from app.services.article_metadata import normalize_title

from .arxiv import Arxiv
from .base import ArticleMeta, OALink, Provider, ProviderError, ProviderHTTP, Ref
from .crossref import Crossref
from .doaj import DOAJ
from .europe_pmc import EuropePMC
from .openalex import OpenAlex
from .repository_search import CoreRepositories
from .unpaywall import Unpaywall

log = logging.getLogger(__name__)
DEFAULT_PRIORITY = {"crossref": 10, "openalex": 20, "unpaywall": 30, "europe_pmc": 40, "doaj": 50, "arxiv": 60, "core": 70}


@dataclass
class LookupResult:
    meta: ArticleMeta | None
    errors: list[ProviderError] = field(default_factory=list)
    attempted: int = 0  # providers that were actually queried


class ProviderManager:
    def __init__(self, settings: Settings | None = None, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings or get_settings()
        classes: list[type[Provider]] = [Crossref, OpenAlex, Unpaywall, EuropePMC, DOAJ, Arxiv, CoreRepositories]
        self.providers: dict[str, Provider] = {}
        self._https: list[ProviderHTTP] = []
        for cls in classes:
            http = ProviderHTTP(cls.name, settings=self.settings, transport=transport,
                                min_interval=3.0 if cls.name == "arxiv" else 0.0)
            self._https.append(http)
            self.providers[cls.name] = cls(http, self.settings)
        self.config: dict[str, dict] = {n: {"enabled": True, "priority": DEFAULT_PRIORITY[n]} for n in self.providers}

    async def aclose(self) -> None:
        for h in self._https:
            await h.aclose()

    def _seed(self) -> dict[str, dict]:
        """Optional config/providers.json: initial enabled/priority values for sources not yet in the database."""
        try:
            raw = json.loads(self.settings.providers_config.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        seed = {}
        for name, cfg in (raw.items() if isinstance(raw, dict) else []):
            if name in self.providers and isinstance(cfg, dict):
                enabled, prio = cfg.get("enabled", True), cfg.get("priority", DEFAULT_PRIORITY[name])
                if isinstance(enabled, bool) and isinstance(prio, int) and not isinstance(prio, bool) and 1 <= prio <= 999:
                    seed[name] = {"enabled": enabled, "priority": prio}
        return seed

    async def load(self) -> None:
        seed = self._seed()
        async with session_scope() as s:
            rows = {r.name: r for r in (await s.execute(select(ProviderConfig))).scalars()}
            for name in self.providers:
                if name not in rows:
                    init = seed.get(name, {"enabled": True, "priority": DEFAULT_PRIORITY[name]})
                    s.add(ProviderConfig(name=name, **init))
                    self.config[name] = dict(init)
                else:
                    self.config[name] = {"enabled": rows[name].enabled, "priority": rows[name].priority}

    def active(self, need_search: bool = False) -> list[Provider]:
        out = [p for n, p in self.providers.items() if self.config[n]["enabled"] and p.is_configured()
               and (p.supports_search or not need_search)]
        return sorted(out, key=lambda p: self.config[p.name]["priority"])

    def priority_of(self, name: str) -> int:
        return self.config.get(name, {}).get("priority", 999)

    def rank_links(self, links: list[OALink]) -> list[OALink]:
        """PDF links first, then best article version, licence known, finally the admin-set provider priority."""
        def key(l: OALink):
            is_pdf, version_rank, has_license, _ = l.score
            return (not is_pdf, -version_rank, not has_license, self.priority_of(l.source))
        return sorted(links, key=key)

    # --- bookkeeping --------------------------------------------------------------------------
    async def _record(self, name: str, ok: bool, latency_ms: int, error: str | None = None, hit: bool = False) -> None:
        try:
            async with session_scope() as s:
                row = await s.get(ProviderConfig, name)
                if row is None:
                    return
                row.total_requests += 1
                row.last_checked_at, row.last_latency_ms = utcnow(), latency_ms
                if ok:
                    row.last_status, row.last_error = "ok", None
                else:
                    row.total_errors += 1
                    row.last_status, row.last_error = "error", (error or "")[:500]
                    s.add(ErrorEvent(kind="provider", source=name, message=(error or "")[:1000]))
                if hit:
                    row.total_hits += 1
        except Exception:  # noqa: BLE001 - never let bookkeeping break a request
            log.exception("could not record provider stats")

    async def record_hit(self, name: str) -> None:
        try:
            async with session_scope() as s:
                row = await s.get(ProviderConfig, name)
                if row:
                    row.total_hits += 1
        except Exception:  # noqa: BLE001
            log.exception("could not record provider hit")

    async def _call(self, p: Provider, coro_factory):
        start = time.monotonic()
        try:
            result = await coro_factory()
        except ProviderError as exc:
            await self._record(p.name, False, int((time.monotonic() - start) * 1000), str(exc))
            raise
        except Exception as exc:  # noqa: BLE001 - provider bug must not take the request down
            log.exception("provider %s crashed", p.name)
            await self._record(p.name, False, int((time.monotonic() - start) * 1000), f"{type(exc).__name__}")
            raise ProviderError(p.name, "bad_response", type(exc).__name__) from exc
        await self._record(p.name, True, int((time.monotonic() - start) * 1000))
        return result

    # --- operations ---------------------------------------------------------------------------
    async def lookup(self, ref: Ref) -> LookupResult:
        errors: list[ProviderError] = []
        results: dict[str, ArticleMeta | None] = {}
        attempted: set[str] = set()

        async def run(p: Provider, r: Ref) -> None:
            attempted.add(p.name)
            try:
                results[p.name] = await self._call(p, lambda: p.lookup(r))
            except ProviderError as exc:
                errors.append(exc)

        providers = self.active()
        await asyncio.gather(*(run(p, ref) for p in providers if p.supports(ref)))
        merged = self._merge([results.get(p.name) for p in providers])
        if merged and merged.doi and not ref.doi:  # a DOI surfaced: let DOI-only providers try now
            new_ref = merged.ref()
            await asyncio.gather(*(run(p, new_ref) for p in providers if p.name not in attempted and p.supports(new_ref)))
            merged = self._merge([results.get(p.name) for p in providers])
        return LookupResult(merged, errors, len(attempted))

    @staticmethod
    def _merge(metas: list[ArticleMeta | None]) -> ArticleMeta | None:
        present = [m for m in metas if m]
        if not present:
            return None
        base = present[0]
        for m in present[1:]:
            base.merge(m)
        return base

    async def search(self, query: str, kind: str, limit: int) -> tuple[list[ArticleMeta], list[ProviderError]]:
        errors: list[ProviderError] = []
        batches: list[list[ArticleMeta]] = []

        async def run(p: Provider) -> None:
            try:
                batches.append(await self._call(p, lambda: p.search(query, kind, limit)))
            except ProviderError as exc:
                errors.append(exc)

        await asyncio.gather(*(run(p) for p in self.active(need_search=True)))
        uniq: list[ArticleMeta] = []
        by_doi: dict[str, ArticleMeta] = {}
        by_title: dict[str, ArticleMeta] = {}
        for batch in batches:
            for m in batch:
                if not m.title:
                    continue
                nt = normalize_title(m.title)
                target = by_doi.get(m.doi) if m.doi else None
                target = target or by_title.get(nt)
                if target is not None and (not m.doi or not target.doi or m.doi == target.doi):
                    target.merge(m)
                else:
                    target = m
                    uniq.append(m)
                if target.doi:
                    by_doi[target.doi] = target
                by_title.setdefault(nt, target)
        qn = normalize_title(query)
        ranked = sorted(uniq, key=lambda m: (
            -(difflib.SequenceMatcher(None, qn, normalize_title(m.title)).ratio() if kind != "author" else 0),
            -len(m.sources), -(m.year or 0)))
        return ranked[:limit], errors

    async def check(self, name: str) -> tuple[bool, int, str | None]:
        p = self.providers[name]
        if not p.is_configured():
            return False, 0, p.config_hint() or "not configured"
        start = time.monotonic()
        try:
            await self._call(p, p.health_check)
            return True, int((time.monotonic() - start) * 1000), None
        except ProviderError as exc:
            return False, int((time.monotonic() - start) * 1000), str(exc)
