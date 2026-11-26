import asyncio
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

import app.core.safe_http as sh
from app.core.safe_http import UnsafeURLError, fetch_limited, is_public_ip, safe_stream, validate_url
from tests.helpers import HostMock, make_pdf


@pytest.mark.parametrize("ip,public", [("93.184.216.34", True), ("8.8.8.8", True), ("127.0.0.1", False), ("10.0.0.5", False),
    ("192.168.1.1", False), ("172.16.0.9", False), ("169.254.169.254", False), ("0.0.0.0", False), ("::1", False),
    ("fe80::1", False), ("fc00::1", False), ("::ffff:127.0.0.1", False), ("224.0.0.1", False), ("100.64.0.1", False)])
def test_ip_classification(ip, public):
    assert is_public_ip(ip) is public


@pytest.mark.parametrize("url", ["ftp://example.org/x", "file:///etc/passwd", "gopher://x", "http://localhost/x", "http://127.0.0.1/",
    "http://[::1]/", "http://10.1.2.3/x", "http://169.254.169.254/latest/meta-data", "http://user:pw@example.org/", "http://example.org:22/",
    "http://foo.internal/", "http://x.localhost/", "", "http:///nohost", "http://0x7f000001/", "http://2130706433/"])
async def test_unsafe_urls_rejected(url):
    with pytest.raises(UnsafeURLError):
        await validate_url(url)


async def test_hostname_resolving_to_private_is_rejected(monkeypatch):
    async def fake(host, port):
        return ["93.184.216.34", "10.0.0.1"]  # one private answer is enough to refuse
    monkeypatch.setattr(sh, "resolve_host", fake)
    with pytest.raises(UnsafeURLError):
        await validate_url("https://evil.example.org/x")


async def test_connection_is_pinned_to_validated_ip(public_dns):
    m = HostMock().on("papers.example.org", lambda r: httpx.Response(200, content=b"ok"))
    body, _, final = await fetch_limited("https://papers.example.org/a.pdf", 1000, transport=m.transport())
    assert body == b"ok"
    req = m.calls[0]
    assert req.url.host == "93.184.216.34" and req.headers["host"] == "papers.example.org"
    assert req.extensions.get("sni_hostname") == "papers.example.org"


async def test_redirect_to_internal_address_is_blocked(public_dns):
    def handler(r):
        return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta-data"})
    m = HostMock().on("papers.example.org", handler)
    with pytest.raises(UnsafeURLError):
        await fetch_limited("https://papers.example.org/redir", 1000, transport=m.transport())


async def test_redirect_loop_and_size_limit(public_dns):
    m = HostMock().on("a.example.org", lambda r: httpx.Response(302, headers={"location": "https://a.example.org/again"}))
    with pytest.raises(UnsafeURLError):
        await fetch_limited("https://a.example.org/x", 1000, transport=m.transport())
    m2 = HostMock().on("a.example.org", lambda r: httpx.Response(200, content=b"x" * 5000))
    with pytest.raises(UnsafeURLError):
        await fetch_limited("https://a.example.org/x", 1000, transport=m2.transport())


class _H(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        data = make_pdf()
        self.send_response(200)
        self.send_header("Content-Type", "application/pdf")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):  # silence
        pass


async def test_real_socket_roundtrip_with_pinning():
    """End-to-end over a real local socket (private addresses are only allowed through the explicit test flag)."""
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}/p.pdf"
        with pytest.raises(UnsafeURLError):
            await fetch_limited(url, 10_000)                       # blocked by default
        body, ctype, _ = await fetch_limited(url, 10_000, allow_private=True)
        assert body.startswith(b"%PDF-") and "pdf" in ctype
    finally:
        srv.shutdown()


async def test_https_pinning_verifies_certificate_against_original_hostname(tmp_path, monkeypatch):
    """TLS goes to the pinned IP but must still be validated for the *hostname* (SNI + cert check)."""
    import shutil, ssl, subprocess
    if not shutil.which("openssl"):
        pytest.skip("openssl not available")
    crt, key = tmp_path / "c.pem", tmp_path / "k.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key), "-out", str(crt), "-days", "2",
                    "-subj", "/CN=papers.test", "-addext", "subjectAltName=DNS:papers.test"], check=True, capture_output=True)

    async def fake_resolve(host, port):
        return ["127.0.0.1"]
    monkeypatch.setattr(sh, "resolve_host", fake_resolve)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _H)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(crt), str(key))
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    monkeypatch.setattr(sh, "ALLOWED_PORTS", {port})
    try:
        ok_ctx = ssl.create_default_context(cafile=str(crt))
        async with safe_stream(f"https://papers.test:{port}/p.pdf", allow_private=True, verify=ok_ctx) as (resp, _):
            assert resp.status_code == 200 and (await resp.aread()).startswith(b"%PDF-")
        # same server, but the URL names a host the certificate does not cover -> verification must fail
        with pytest.raises(httpx.ConnectError):
            async with safe_stream(f"https://other.test:{port}/p.pdf", allow_private=True, verify=ok_ctx) as (resp, _):
                await resp.aread()
    finally:
        srv.shutdown()
