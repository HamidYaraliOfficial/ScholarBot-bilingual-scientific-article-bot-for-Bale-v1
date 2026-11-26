"""Downloads PDFs from *legal open-access* links only, with validation and no access-control bypass."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urljoin

import httpx

from app.core.compat import run_blocking
from app.core.config import Settings, get_settings
from app.core.safe_http import UnsafeURLError, fetch_limited, safe_stream
from app.providers.base import OALink
from app.services.pdf_validator import validate_pdf
from app.services.storage_manager import StorageManager

log = logging.getLogger(__name__)
MAX_LANDING_BYTES = 2_000_000
MAX_LANDING_PAGES = 2
MAX_ATTEMPTS = 8


@dataclass
class DownloadResult:
    part_path: object
    sha256: str
    size: int
    pages: int | None
    link: OALink
    final_url: str


@dataclass
class DownloadOutcome:
    result: DownloadResult | None = None
    failures: list[tuple[str, str]] = field(default_factory=list)  # (url, reason)

    @property
    def reason(self) -> str:
        reasons = [r for _, r in self.failures]
        for r in ("too_large", "not_pdf", "html_page", "http_error", "unsafe_url", "timeout"):
            if r in reasons:
                return r
        return reasons[0] if reasons else "no_links"


class _MetaParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.meta: dict[str, list[str]] = {}
        self.alt_pdf: str | None = None

    def handle_starttag(self, tag, attrs):  # noqa: ANN001
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "meta" and a.get("name") and a.get("content"):
            self.meta.setdefault(a["name"].lower(), []).append(a["content"].strip())
        elif tag == "link" and a.get("type") == "application/pdf" and a.get("href") and not self.alt_pdf:
            self.alt_pdf = a["href"]


def parse_landing_html(html: str) -> _MetaParser:
    p = _MetaParser()
    try:
        p.feed(html)
    except Exception:  # noqa: BLE001 - tolerate broken markup
        pass
    return p


def pdf_url_from_html(html: str, base_url: str) -> str | None:
    p = parse_landing_html(html)
    cand = (p.meta.get("citation_pdf_url") or [None])[0] or p.alt_pdf
    return urljoin(base_url, cand) if cand else None


class PDFDownloader:
    def __init__(self, storage: StorageManager, settings: Settings | None = None,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.storage, self.s, self.transport = storage, settings or get_settings(), transport

    async def download_first_valid(self, links: list[OALink], max_mb: int) -> DownloadOutcome:
        out = DownloadOutcome()
        max_bytes = max_mb * 1024 * 1024
        landing_tried = attempts = 0
        for link in links:
            if attempts >= MAX_ATTEMPTS:
                break
            urls = [link.url]
            if link.kind == "landing":
                if landing_tried >= MAX_LANDING_PAGES:
                    continue
                landing_tried += 1
                pdf_url = await self._pdf_from_landing(link.url, out)
                if not pdf_url:
                    continue
                urls = [pdf_url]
            for url in urls:
                attempts += 1
                res = await self._try(link, url, max_bytes, out)
                if res:
                    out.result = res
                    return out
        return out

    async def _pdf_from_landing(self, url: str, out: DownloadOutcome) -> str | None:
        try:
            body, ctype, final = await fetch_limited(url, MAX_LANDING_BYTES, timeout=20, allow_private=self.s.allow_private_urls,
                                                     transport=self.transport, headers={"Accept": "text/html,*/*"})
        except (UnsafeURLError, httpx.HTTPError) as exc:
            out.failures.append((url, "unsafe_url" if isinstance(exc, UnsafeURLError) else "http_error"))
            return None
        if "pdf" in ctype.lower() and body.startswith(b"%PDF"):
            return final
        found = pdf_url_from_html(body.decode("utf-8", "ignore"), final)
        if not found:
            out.failures.append((url, "html_page"))
        return found

    async def _try(self, link: OALink, url: str, max_bytes: int, out: DownloadOutcome) -> DownloadResult | None:
        part = self.storage.part_path()
        try:
            async with safe_stream(url, timeout=45, allow_private=self.s.allow_private_urls, transport=self.transport,
                                   headers={"Accept": "application/pdf,*/*;q=0.8"}) as (resp, final_url):
                if resp.status_code >= 400:
                    out.failures.append((url, "http_error"))
                    return None
                declared = int(resp.headers.get("content-length") or 0)
                if declared > max_bytes:
                    out.failures.append((url, "too_large"))
                    return None
                import hashlib
                h, size, first = hashlib.sha256(), 0, True
                with part.open("wb") as f:
                    async for chunk in resp.aiter_bytes(64 * 1024):
                        if first:
                            first = False
                            if b"%PDF-" not in chunk[:1024]:
                                out.failures.append((url, "html_page" if chunk.lstrip()[:1] == b"<" else "not_pdf"))
                                part.unlink(missing_ok=True)
                                return None
                        size += len(chunk)
                        if size > max_bytes:
                            out.failures.append((url, "too_large"))
                            part.unlink(missing_ok=True)
                            return None
                        h.update(chunk)
                        f.write(chunk)
            check = await run_blocking(validate_pdf, part, max_bytes)
            if not check.ok:
                out.failures.append((url, check.reason or "corrupt"))
                part.unlink(missing_ok=True)
                return None
            return DownloadResult(part, h.hexdigest(), size, check.pages, link, final_url)
        except UnsafeURLError as exc:
            log.warning("blocked unsafe URL (%s)", exc)
            out.failures.append((url, "unsafe_url"))
        except httpx.TimeoutException:
            out.failures.append((url, "timeout"))
        except httpx.HTTPError as exc:
            out.failures.append((url, "http_error"))
            log.info("download failed: %s", type(exc).__name__)
        part.unlink(missing_ok=True)
        return None
