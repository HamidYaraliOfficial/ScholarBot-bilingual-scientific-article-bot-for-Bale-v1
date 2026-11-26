"""Detect what the user sent: DOI, arXiv id, PubMed/PMC id, URL, author or title."""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from app.providers.arxiv import arxiv_id_from_doi, arxiv_id_from_text
from app.providers.base import DOI_RE, Ref, normalize_doi

URL_RE = re.compile(r"^https?://\S+$", re.I)
PMC_RE = re.compile(r"\bPMC\s?(\d{4,9})\b", re.I)
PMID_RE = re.compile(r"(?:pubmed\.ncbi\.nlm\.nih\.gov/|\bPMID[:\s]+|europepmc\.org/article/MED/)(\d{4,9})", re.I)
MAX_QUERY_LEN = 500


class InvalidInput(ValueError):
    """code is a localization key suffix (err_<code>)."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass
class ParsedQuery:
    kind: str  # doi | arxiv | pmid | pmcid | url | title | author
    value: str
    ref: Ref | None = None

    @property
    def key(self) -> str:
        """Stable identity used to coalesce identical in-flight requests."""
        return f"{self.kind}:{self.value.lower()}"


def parse_input(text: str, hint: str | None = None) -> ParsedQuery:
    """`hint` may be 'doi', 'link', 'author', 'title' (set by the bot menu state)."""
    text = (text or "").strip()
    if not text:
        raise InvalidInput("empty_query")
    if len(text) > MAX_QUERY_LEN:
        raise InvalidInput("query_too_long")
    if hint == "author":
        if len(text) < 3:
            raise InvalidInput("query_too_short")
        return ParsedQuery("author", " ".join(text.split()))

    doi = normalize_doi(text)
    if hint == "doi" and not doi:
        raise InvalidInput("invalid_doi")
    if doi:
        ax = arxiv_id_from_doi(doi)
        return ParsedQuery("doi", doi, Ref(doi=doi, arxiv_id=ax))
    if aid := arxiv_id_from_text(text):
        return ParsedQuery("arxiv", aid, Ref(arxiv_id=aid))
    if m := PMC_RE.search(text):
        return ParsedQuery("pmcid", f"PMC{m.group(1)}", Ref(pmcid=f"PMC{m.group(1)}"))
    if m := PMID_RE.search(text):
        return ParsedQuery("pmid", m.group(1), Ref(pmid=m.group(1)))
    if URL_RE.match(text):
        if not urlsplit(text).hostname:
            raise InvalidInput("invalid_link")
        return ParsedQuery("url", text)
    if hint == "link":
        raise InvalidInput("invalid_link")
    if text.lower().startswith(("http://", "https://", "www.")):
        raise InvalidInput("invalid_link")
    if len(text) < 4:
        raise InvalidInput("query_too_short")
    return ParsedQuery("title", " ".join(text.split()))


def doi_in_url(url: str) -> str | None:
    return normalize_doi(url) if DOI_RE.search(url) else None
