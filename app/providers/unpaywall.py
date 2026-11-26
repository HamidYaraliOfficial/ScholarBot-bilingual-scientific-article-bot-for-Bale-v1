"""Unpaywall — legal open-access locations for a DOI (requires a contact e-mail)."""
from __future__ import annotations

from urllib.parse import quote

from .base import ArticleMeta, OALink, Provider, Ref

BASE = "https://api.unpaywall.org/v2"


class Unpaywall(Provider):
    name = "unpaywall"
    key_env = "CONTACT_EMAIL"
    key_required = True
    description = "Unpaywall — open-access locations by DOI"

    def is_configured(self) -> bool:
        return bool(self.settings.contact_email)

    def config_hint(self) -> str:
        return "" if self.is_configured() else "Set CONTACT_EMAIL in .env (required by Unpaywall)"

    async def lookup(self, ref: Ref) -> ArticleMeta | None:
        if not ref.doi:
            return None
        data = await self.http.get_json(f"{BASE}/{quote(ref.doi, safe='/()')}", {"email": self.settings.contact_email},
                                        allow_404=True)
        if not data:
            return None
        links: list[OALink] = []
        for loc in data.get("oa_locations") or []:
            ht = loc.get("host_type")
            if loc.get("url_for_pdf"):
                links.append(OALink(loc["url_for_pdf"], self.name, "pdf", loc.get("version"), loc.get("license"), ht))
            elif loc.get("url_for_landing_page"):
                links.append(OALink(loc["url_for_landing_page"], self.name, "landing", loc.get("version"),
                                    loc.get("license"), ht))
        authors = [" ".join(x for x in (a.get("given"), a.get("family")) if x) or a.get("raw_author_name", "")
                   for a in data.get("z_authors") or []]
        best = data.get("best_oa_location") or {}
        return ArticleMeta(
            title=data.get("title") or "", authors=[a for a in authors if a], year=data.get("year"),
            journal=data.get("journal_name"), publisher=data.get("publisher"), work_type=data.get("genre"),
            doi=(data.get("doi") or ref.doi).lower(), access_status="open" if data.get("is_oa") else "closed",
            license=best.get("license"), landing_url=data.get("doi_url"), sources=[self.name], links=links)

    async def health_check(self) -> None:
        if not self.is_configured():
            return
        await self.http.get_json(f"{BASE}/10.1038/nature12373", {"email": self.settings.contact_email})
