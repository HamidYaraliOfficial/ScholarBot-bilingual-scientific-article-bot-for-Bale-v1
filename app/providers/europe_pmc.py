"""Europe PMC REST API — metadata and open-access full text (mirrors PubMed Central)."""
from __future__ import annotations

from .base import ArticleMeta, OALink, Provider, Ref, normalize_doi, strip_tags

BASE = "https://www.ebi.ac.uk/europepmc/webservices/rest"
RENDER = "https://europepmc.org/backend/ptpmcrender.fcgi"


def parse_result(r: dict) -> ArticleMeta:
    ji = r.get("journalInfo") or {}
    journal = (ji.get("journal") or {}).get("title") or r.get("journalTitle")
    authors = [a.get("fullName") for a in ((r.get("authorList") or {}).get("author") or []) if a.get("fullName")]
    if not authors and r.get("authorString"):
        authors = [x.strip() for x in r["authorString"].rstrip(".").split(",") if x.strip()]
    is_oa = r.get("isOpenAccess") == "Y"
    pmcid = r.get("pmcid")
    links: list[OALink] = []
    for u in ((r.get("fullTextUrlList") or {}).get("fullTextUrl") or []):
        if u.get("availabilityCode") == "OA" and u.get("url"):
            style = (u.get("documentStyle") or "").lower()
            if style in ("pdf", "html"):
                links.append(OALink(u["url"], "europe_pmc", "pdf" if style == "pdf" else "landing",
                                    license=r.get("license"), host_type="repository"))
    if pmcid and is_oa:
        links.append(OALink(f"{RENDER}?accid={pmcid}&blobtype=pdf", "europe_pmc", "pdf", "publishedVersion",
                            r.get("license"), "repository"))
    return ArticleMeta(
        title=(strip_tags(r.get("title")) or "").rstrip("."), authors=authors,
        year=int(r["pubYear"]) if str(r.get("pubYear", "")).isdigit() else None, journal=journal,
        volume=ji.get("volume"), issue=ji.get("issue"), pages=r.get("pageInfo"),
        issn=(ji.get("journal") or {}).get("issn"), abstract=strip_tags(r.get("abstractText")),
        keywords=((r.get("keywordList") or {}).get("keyword") or []), work_type=(r.get("pubTypeList") or {}).get(
            "pubType", [None])[0] if isinstance((r.get("pubTypeList") or {}).get("pubType"), list) else None,
        doi=normalize_doi(r.get("doi")), access_status="open" if is_oa else "unknown", license=r.get("license"),
        pmid=r.get("pmid"), pmcid=pmcid, sources=["europe_pmc"], links=links,
        landing_url=f"https://europepmc.org/article/{r.get('source')}/{r.get('id')}" if r.get("id") else None)


class EuropePMC(Provider):
    name = "europe_pmc"
    supports_search = True
    description = "Europe PMC — biomedical literature, PubMed Central open-access full text"

    async def _query(self, query: str, limit: int) -> list[ArticleMeta]:
        data = await self.http.get_json(f"{BASE}/search", {"query": query, "format": "json", "resultType": "core",
                                                           "pageSize": limit})
        return [parse_result(r) for r in ((data or {}).get("resultList") or {}).get("result", [])]

    def supports(self, ref: Ref) -> bool:
        return bool(ref.doi or ref.pmcid or ref.pmid)

    async def lookup(self, ref: Ref) -> ArticleMeta | None:
        if ref.doi:
            q = f'DOI:"{ref.doi}"'
        elif ref.pmcid:
            q = f"PMCID:{ref.pmcid}"
        elif ref.pmid:
            q = f"EXT_ID:{ref.pmid} AND SRC:MED"
        else:
            return None
        res = await self._query(q, 1)
        return res[0] if res else None

    async def search(self, query: str, kind: str, limit: int) -> list[ArticleMeta]:
        clean = query.replace('"', " ")
        return await self._query(f'AUTH:"{clean}"' if kind == "author" else f'TITLE:"{clean}"', limit) or \
            (await self._query(clean, limit) if kind != "author" else [])

    async def health_check(self) -> None:
        await self._query("DOI:10.1371/journal.pone.0213676", 1)
