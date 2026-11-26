"""CSV / Excel / BibTeX / RIS exports of article rows."""
from __future__ import annotations

import csv
import io
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill

from app.services.article_metadata import to_bibtex, to_ris

COLUMNS = ["id", "title", "authors", "year", "journal", "volume", "issue", "pages", "doi", "publisher", "status", "library",
           "source", "license", "access", "size_bytes", "requested_by", "requests", "created_at", "url"]


def _safe_cell(v: Any) -> Any:
    """Neutralise spreadsheet formula injection."""
    if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + v
    return v


def rows(articles: list[Any]) -> list[list[Any]]:
    out = []
    for a in articles:
        user = a.last_user
        who = (f"@{user.username}" if user and user.username else str(user.bale_id) if user else "")
        out.append([a.id, a.title, "; ".join(a.authors or []), a.year, a.journal, a.volume, a.issue, a.pages, a.doi,
                    a.publisher, a.status, "saved" if a.saved else "temporary" if a.file else "", a.source, a.license,
                    a.access_status, a.file.size_bytes if a.file else None, who, a.request_count,
                    a.created_at.isoformat(sep=" ", timespec="seconds") if a.created_at else "", a.doi_url or a.landing_url])
    return out


def to_csv(articles: list[Any]) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(COLUMNS)
    for r in rows(articles):
        w.writerow([_safe_cell(c) for c in r])
    return buf.getvalue().encode("utf-8-sig")  # BOM so Excel opens UTF-8 (Persian) correctly


def to_xlsx(articles: list[Any]) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Articles"
    ws.append(COLUMNS)
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="1F3A5F")
        c.alignment = Alignment(horizontal="center")
    for r in rows(articles):
        ws.append([_safe_cell(c) for c in r])
    widths = {"title": 60, "authors": 40, "journal": 30, "doi": 30, "url": 40}
    for i, name in enumerate(COLUMNS, 1):
        ws.column_dimensions[ws.cell(1, i).column_letter].width = widths.get(name, 14)
    ws.freeze_panes = "A2"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def export(articles: list[Any], fmt: str) -> tuple[bytes, str, str]:
    """Returns (content, mime, extension)."""
    if fmt == "csv":
        return to_csv(articles), "text/csv; charset=utf-8", "csv"
    if fmt == "xlsx":
        return to_xlsx(articles), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "xlsx"
    if fmt == "bibtex":
        return to_bibtex(articles).encode(), "application/x-bibtex; charset=utf-8", "bib"
    if fmt == "ris":
        return to_ris(articles).encode(), "application/x-research-info-systems; charset=utf-8", "ris"
    raise ValueError("unknown export format")
