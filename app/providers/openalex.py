"""OpenAlex — metadata plus open-access locations. A free API key is required since Feb 2026."""
from __future__ import annotations

from .base import ArticleMeta, OALink, Provider, Ref, normalize_doi

BASE = "https://api.openalex.org"


def _abstract(inv: dict | None) -> str | None:
    if not inv:
        return None
    words: list[tuple[int, str]] = [(p, w) for w, positions in inv.items() for p in positions]
    return " ".join(w for _, w in sorted(words)) or None


def parse_work(w: dict) -> ArticleMeta:
    loc = w.get("primary_location") or {}
    src = loc.get("source") or {}
    biblio = w.get("biblio") or {}
    pages = "-".join(x for x in (biblio.get("first_page"), biblio.get("last_page")) if x) or None
    links: list[OALink] = []
    for l in w.get("locations") or []:
        if not l.get("is_oa"):
            continue
        host = (l.get("source") or {}).get("type")
        host_type = "repository" if host == "repository" else "publisher" if host else None
        if l.get("pdf_url"):
            links.append(OALink(l["pdf_url"], "openalex", "pdf", l.get("version"), l.get("license"), host_type))
        elif l.get("landing_page_url"):
            links.append(OALink(l["landing_page_url"], "openalex", "landing", l.get("version"), l.get("license"), host_type))
    oa = w.get("open_access") or {}
    ids = w.get("ids") or {}
    pmid = (ids.get("pmid") or "").rsplit("/", 1)[-1] or None
    pmcid = (ids.get("pmcid") or "").rsplit("/", 1)[-1] or None
    best = w.get("best_oa_location") or {}
    return ArticleMeta(
        title=w.get("title") or w.get("display_name") or "",
        authors=[a["author"]["display_name"] for a in w.get("authorships") or [] if a.get("author", {}).get("display_name")],
        year=w.get("publication_year"), journal=src.get("display_name"), volume=biblio.get("volume"),
        issue=biblio.get("issue"), pages=pages, issn=src.get("issn_l") or (src.get("issn") or [None])[0],
        publisher=src.get("host_organization_name"), abstract=_abstract(w.get("abstract_inverted_index")),
        keywords=[k["display_name"] for k in w.get("keywords") or [] if k.get("display_name")],
        work_type=w.get("type"), doi=normalize_doi(w.get("doi")), landing_url=loc.get("landing_page_url"),
        access_status="open" if oa.get("is_oa") else "closed" if "is_oa" in oa else "unknown",
        license=best.get("license"), pmid=pmid, pmcid=pmcid, openalex_id=(w.get("id") or "").rsplit("/", 1)[-1] or None,
        sources=["openalex"], links=links)


class OpenAlex(Provider):
    name = "openalex"
    supports_search = True
    key_env = "OPENALEX_API_KEY"
    description = "OpenAlex — metadata and open-access locations"

    def config_hint(self) -> str:
        return "" if self.settings.openalex_api_key else "OPENALEX_API_KEY missing: OpenAlex may reject keyless requests"

    def _params(self, **kw) -> dict:
        if self.settings.openalex_api_key:
            kw["api_key"] = self.settings.openalex_api_key
        return kw

    def supports(self, ref: Ref) -> bool:
        return bool(ref.doi or ref.pmid or ref.pmcid or ref.openalex_id)

    async def lookup(self, ref: Ref) -> ArticleMeta | None:
        if ref.doi:
            flt = f"doi:https://doi.org/{ref.doi}"
        elif ref.pmid:
            flt = f"pmid:{ref.pmid}"
        elif ref.pmcid:
            flt = f"pmcid:{ref.pmcid}"
        elif ref.openalex_id:
            flt = f"openalex:{ref.openalex_id}"
        else:
            return None
        data = await self.http.get_json(f"{BASE}/works", self._params(filter=flt, **{"per-page": 1}))
        results = (data or {}).get("results") or []
        return parse_work(results[0]) if results else None

    async def search(self, query: str, kind: str, limit: int) -> list[ArticleMeta]:
        # OpenAlex full-text `search` covers titles/abstracts; author names are matched through raw_author_name.
        clean = query.replace(",", " ").replace('"', " ").strip()
        params = {"filter": f'raw_author_name.search:"{clean}"'} if kind == "author" else {"search": query}
        data = await self.http.get_json(f"{BASE}/works", self._params(**params, **{"per-page": limit}))
        return [parse_work(w) for w in (data or {}).get("results", [])]

    async def health_check(self) -> None:
        await self.http.get_json(f"{BASE}/works", self._params(**{"per-page": 1, "select": "id"}))
