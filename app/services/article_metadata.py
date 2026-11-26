"""Metadata helpers: identity hashing, link ranking/filtering and citation export formats."""
from __future__ import annotations

import hashlib
import re
import unicodedata
from typing import Any

from app.providers.base import OALink

_BLOCKED_LICENSE_TOKENS = ("all-rights-reserved", "closed", "paywall")


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def normalize_title(title: str) -> str:
    return _norm(title)


def title_hash(title: str, authors: list[str] | None, year: int | None) -> str | None:
    t = _norm(title)
    if len(t) < 8:
        return None
    first = _norm((authors or [""])[0]).split(" ")[-1] if authors else ""
    return hashlib.sha1(f"{t}|{first}|{year or ''}".encode()).hexdigest()


def usable_links(links: list[OALink], require_license: bool) -> list[OALink]:
    """Drop links with explicitly restricted licences (and, optionally, those without any licence info)."""
    out = []
    for l in links:
        lic = (l.license or "").lower()
        if any(tok in lic for tok in _BLOCKED_LICENSE_TOKENS):
            continue
        if require_license and not lic:
            continue
        out.append(l)
    return out


# --- citation formats ---------------------------------------------------------------------------
def _g(a: Any, name: str, default=None):
    return getattr(a, name, default) if not isinstance(a, dict) else a.get(name, default)


def _bib_escape(s: str) -> str:
    return re.sub(r"([&%$#_])", r"\\\1", (s or "").replace("{", "").replace("}", ""))


def bibtex_key(a: Any) -> str:
    authors = _g(a, "authors") or []
    surname = (_norm(authors[0]).split(" ")[-1] if authors else "anon") or "anon"
    word = next((w for w in _norm(_g(a, "title", "")).split() if len(w) > 3), "paper")
    return f"{surname}{_g(a, 'year') or ''}{word}"


def to_bibtex(articles: list[Any]) -> str:
    entries = []
    for a in articles:
        wt = (_g(a, "work_type") or "").lower()
        etype = "inproceedings" if "proceedings" in wt else "misc" if wt in ("preprint", "posted-content") else "article"
        fields = {
            "author": " and ".join(_bib_escape(x) for x in _g(a, "authors") or []),
            "title": "{" + _bib_escape(_g(a, "title", "")) + "}",
            "journal" if etype == "article" else "booktitle" if etype == "inproceedings" else "howpublished":
                _bib_escape(_g(a, "journal") or ""),
            "year": str(_g(a, "year") or ""), "volume": _g(a, "volume") or "", "number": _g(a, "issue") or "",
            "pages": (_g(a, "pages") or "").replace("-", "--") if "--" not in (_g(a, "pages") or "") else _g(a, "pages"),
            "publisher": _bib_escape(_g(a, "publisher") or ""), "doi": _g(a, "doi") or "",
            "url": _g(a, "doi_url") or _g(a, "landing_url") or "",
        }
        body = ",\n".join(f"  {k} = {{{v}}}" for k, v in fields.items() if v)
        entries.append(f"@{etype}{{{bibtex_key(a)},\n{body}\n}}")
    return "\n\n".join(entries) + ("\n" if entries else "")


def to_ris(articles: list[Any]) -> str:
    out: list[str] = []
    for a in articles:
        wt = (_g(a, "work_type") or "").lower()
        out.append("TY  - " + ("CPAPER" if "proceedings" in wt else "UNPB" if wt in ("preprint", "posted-content") else "JOUR"))
        out += [f"AU  - {x}" for x in _g(a, "authors") or []]
        pages = (_g(a, "pages") or "")
        sp, _, ep = pages.partition("-")
        for tag, val in (("TI", _g(a, "title")), ("T2", _g(a, "journal")), ("PY", _g(a, "year")), ("VL", _g(a, "volume")),
                         ("IS", _g(a, "issue")), ("SP", sp), ("EP", ep), ("DO", _g(a, "doi")), ("PB", _g(a, "publisher")),
                         ("SN", _g(a, "issn")), ("UR", _g(a, "doi_url") or _g(a, "landing_url")), ("AB", _g(a, "abstract"))):
            if val:
                out.append(f"{tag}  - {val}")
        out += [f"KW  - {k}" for k in _g(a, "keywords") or []]
        out.append("ER  - \n")
    return "\r\n".join(out)
