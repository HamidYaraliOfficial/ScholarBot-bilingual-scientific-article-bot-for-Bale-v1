"""Shared test helpers: fake external services (providers, repositories, Bale) built on httpx.MockTransport."""
from __future__ import annotations

import io
import json
import re
from typing import Callable

import httpx
from pypdf import PdfWriter


def make_pdf(pages: int = 1, extra: bytes = b"") -> bytes:
    w = PdfWriter()
    for _ in range(pages):
        w.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue() + extra


def jresp(data, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=data)


class HostMock:
    """Routes requests by host to handler(request) -> Response|None; unknown hosts/paths give 404."""

    def __init__(self) -> None:
        self.handlers: dict[str, Callable[[httpx.Request], httpx.Response | None]] = {}
        self.calls: list[httpx.Request] = []

    def on(self, host: str, handler) -> "HostMock":
        self.handlers[host] = handler
        return self

    def count(self, host: str, contains: str = "") -> int:
        return sum(1 for r in self.calls if (r.headers.get("host") or r.url.host) == host and contains in str(r.url))

    def transport(self) -> httpx.MockTransport:
        def handle(request: httpx.Request) -> httpx.Response:
            self.calls.append(request)
            host = request.headers.get("host") or request.url.host  # pinned requests carry the real name in Host
            h = self.handlers.get(host.split(":")[0])
            resp = h(request) if h else None
            return resp if resp is not None else httpx.Response(404, json={"message": "not found"})
        return httpx.MockTransport(handle)


DOI = "10.1234/abc"
CROSSREF_WORK = {"status": "ok", "message": {
    "DOI": DOI, "title": ["Deep Learning for Open Science"], "author": [{"given": "Ada", "family": "Lovelace"}, {"given": "Alan", "family": "Turing"}],
    "issued": {"date-parts": [[2024, 5, 1]]}, "container-title": ["Journal of Open Research"], "volume": "12", "issue": "3",
    "page": "100-110", "publisher": "Open Press", "type": "journal-article", "ISSN": ["1234-5678"], "subject": ["Computer Science"],
    "abstract": "<jats:p>An <b>abstract</b>.</jats:p>", "URL": f"https://doi.org/{DOI}"}}
UNPAYWALL_OA = {"doi": DOI, "is_oa": True, "title": "Deep Learning for Open Science", "year": 2024, "journal_name": "Journal of Open Research",
                "best_oa_location": {"url_for_pdf": "https://repo.example.org/paper.pdf", "license": "cc-by", "version": "publishedVersion", "host_type": "repository"},
                "oa_locations": [{"url_for_pdf": "https://repo.example.org/paper.pdf", "url_for_landing_page": "https://repo.example.org/handle/1", "license": "cc-by",
                                  "version": "publishedVersion", "host_type": "repository"}],
                "z_authors": [{"given": "Ada", "family": "Lovelace"}]}
UNPAYWALL_CLOSED = {"doi": DOI, "is_oa": False, "title": "Deep Learning for Open Science", "year": 2024, "oa_locations": [], "best_oa_location": None}


def standard_providers(pdf_url_doi: str = DOI, oa: dict | None = UNPAYWALL_OA, crossref: dict | None = CROSSREF_WORK) -> HostMock:
    """Crossref knows the article; Unpaywall returns `oa`; everything else answers 404/empty."""
    m = HostMock()
    m.on("api.crossref.org", lambda r: jresp(crossref) if crossref and "/works/" in r.url.path else (
        jresp({"message": {"items": [crossref["message"]]}}) if crossref else jresp({"message": {"items": []}})))
    m.on("api.unpaywall.org", lambda r: jresp(oa) if oa else None)
    m.on("api.openalex.org", lambda r: jresp({"results": []}))
    m.on("www.ebi.ac.uk", lambda r: jresp({"resultList": {"result": []}}))
    m.on("doaj.org", lambda r: jresp({"results": []}))
    m.on("export.arxiv.org", lambda r: httpx.Response(200, text='<feed xmlns="http://www.w3.org/2005/Atom"></feed>'))
    return m


def pdf_host(pdf: bytes | None = None, html: bool = False, status: int = 200) -> HostMock:
    pdf = pdf if pdf is not None else make_pdf(2)

    def serve(r: httpx.Request):
        if status != 200:
            return httpx.Response(status)
        if html:
            return httpx.Response(200, text="<html><body>Please log in</body></html>", headers={"content-type": "text/html"})
        return httpx.Response(200, content=pdf, headers={"content-type": "application/pdf"})
    m = HostMock()
    m.on("repo.example.org", serve)
    return m


class FakeBale:
    """Minimal Bale Bot API double: records calls, serves queued updates."""

    def __init__(self, fail: dict[str, str] | None = None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.documents: list[dict] = []
        self.updates: list[dict] = []
        self._id = 100
        self.fail = fail or {}

    def push_message(self, text: str, user_id: int = 555, username: str = "alice") -> None:
        self.updates.append({"update_id": len(self.updates) + 1, "message": {"message_id": 1, "chat": {"id": user_id, "type": "private"},
                             "from": {"id": user_id, "is_bot": False, "first_name": "Alice", "username": username}, "text": text}})

    def push_callback(self, data: str, user_id: int = 555) -> None:
        self.updates.append({"update_id": len(self.updates) + 1, "callback_query": {"id": "cb", "data": data,
                             "from": {"id": user_id, "is_bot": False, "first_name": "Alice"}, "message": {"chat": {"id": user_id}}}})

    def sent_texts(self, chat: int | None = None) -> list[str]:
        return [p.get("text", "") for m, p in self.calls if m == "sendMessage" and (chat is None or p.get("chat_id") == chat)]

    def transport(self) -> httpx.MockTransport:
        def handle(request: httpx.Request) -> httpx.Response:
            method = request.url.path.rsplit("/", 1)[-1]
            ctype = request.headers.get("content-type", "")
            if ctype.startswith("multipart"):
                body = request.content
                name = re.search(rb'filename="([^"]+)"', body)
                payload = {"chat_id": int(re.search(rb'name="chat_id"\r\n\r\n(\d+)', body).group(1)),
                           "filename": name.group(1).decode() if name else None, "has_pdf": b"%PDF-" in body,
                           "caption": (re.search(rb'name="caption"\r\n\r\n(.*?)\r\n--', body, re.S) or [None, b""])[1].decode()}
                self.documents.append(payload)
            else:
                payload = json.loads(request.content or b"{}")
            self.calls.append((method, payload))
            if method in self.fail:
                return httpx.Response(400, json={"ok": False, "error_code": 400, "description": self.fail[method]})
            if method == "getMe":
                return jresp({"ok": True, "result": {"id": 1, "is_bot": True, "username": "scholar_test_bot"}})
            if method == "getUpdates":
                off = payload.get("offset") or 0
                return jresp({"ok": True, "result": [u for u in self.updates if u["update_id"] >= off]})
            self._id += 1
            return jresp({"ok": True, "result": {"message_id": self._id, "chat": {"id": payload.get("chat_id")}}})
        return httpx.MockTransport(handle)
