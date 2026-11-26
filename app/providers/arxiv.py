"""arXiv API (Atom). Preprints are freely downloadable."""
from __future__ import annotations

import re

from defusedxml import ElementTree as ET

from .base import ARXIV_OLD_RE, ArticleMeta, OALink, Provider, Ref, normalize_doi, strip_tags

BASE = "https://export.arxiv.org/api/query"
NS = {"a": "http://www.w3.org/2005/Atom", "x": "http://arxiv.org/schemas/atom"}


def arxiv_id_from_text(text: str) -> str | None:
    text = text.strip()
    m = re.search(r"arxiv\.org/(?:abs|pdf)/([^\s?#]+?)(?:\.pdf)?(?:$|[?#\s])", text + " ", re.I)
    cand = m.group(1) if m else re.sub(r"^arxiv:", "", text, flags=re.I)
    cand = re.sub(r"v\d+$", "", cand)
    if re.fullmatch(r"\d{4}\.\d{4,5}", cand) or re.fullmatch(ARXIV_OLD_RE.pattern.replace("(v\\d+)?", ""), cand, re.I):
        return cand
    return None


def arxiv_id_from_doi(doi: str | None) -> str | None:
    m = re.match(r"10\.48550/arxiv\.(.+)$", doi or "", re.I)
    return re.sub(r"v\d+$", "", m.group(1)) if m else None


def parse_feed(xml_text: str) -> list[ArticleMeta]:
    root = ET.fromstring(xml_text)
    out: list[ArticleMeta] = []
    for e in root.findall("a:entry", NS):
        abs_url = (e.findtext("a:id", "", NS) or "").strip()
        if "arxiv.org/abs/" not in abs_url:
            continue  # error entries
        aid = re.sub(r"v\d+$", "", abs_url.split("/abs/")[1])
        published = e.findtext("a:published", "", NS)
        links = [OALink(f"https://arxiv.org/pdf/{aid}", "arxiv", "pdf", "submittedVersion", None, "repository")]
        doi = normalize_doi(e.findtext("x:doi", "", NS)) or f"10.48550/arxiv.{aid}".lower()
        out.append(ArticleMeta(
            title=strip_tags(e.findtext("a:title", "", NS)) or "",
            authors=[(a.findtext("a:name", "", NS) or "").strip() for a in e.findall("a:author", NS)],
            year=int(published[:4]) if published[:4].isdigit() else None,
            journal=e.findtext("x:journal_ref", None, NS) or "arXiv", abstract=strip_tags(e.findtext("a:summary", "", NS)),
            keywords=[c.get("term") for c in e.findall("a:category", NS) if c.get("term")], work_type="preprint",
            doi=doi, landing_url=abs_url, access_status="open", arxiv_id=aid, sources=["arxiv"], links=links,
            publisher="arXiv"))
    return out


class Arxiv(Provider):
    name = "arxiv"
    supports_search = True
    description = "arXiv — open preprints"

    def __init__(self, http, settings=None):
        super().__init__(http, settings)

    async def _fetch(self, params: dict) -> list[ArticleMeta]:
        resp = await self.http.get(BASE, params, headers={"Accept": "application/atom+xml"})
        try:
            return parse_feed(resp.text)
        except ET.ParseError as exc:  # type: ignore[attr-defined]
            from .base import ProviderError
            raise ProviderError(self.name, "bad_response", "invalid Atom feed") from exc

    def supports(self, ref: Ref) -> bool:
        return bool(ref.arxiv_id or arxiv_id_from_doi(ref.doi))

    async def lookup(self, ref: Ref) -> ArticleMeta | None:
        aid = ref.arxiv_id or arxiv_id_from_doi(ref.doi)
        if not aid:
            return None
        res = await self._fetch({"id_list": aid, "max_results": 1})
        return res[0] if res else None

    async def search(self, query: str, kind: str, limit: int) -> list[ArticleMeta]:
        clean = query.replace('"', " ")
        field = "au" if kind == "author" else "ti"
        return await self._fetch({"search_query": f'{field}:"{clean}"', "max_results": limit,
                                  "sortBy": "relevance"})

    async def health_check(self) -> None:
        await self._fetch({"search_query": "all:electron", "max_results": 1})
