"""SSRF-safe outbound fetching for user-influenced URLs.

Every hop (including redirects) is validated: http(s) only, no credentials, standard ports,
and *all* resolved addresses must be public. The validated IP is then pinned for the actual
connection (Host header + TLS SNI keep the original name), which closes DNS-rebinding gaps.
"""
from __future__ import annotations

import asyncio
import ipaddress
import socket
from contextlib import asynccontextmanager
from typing import AsyncIterator
from urllib.parse import urljoin, urlsplit

import httpx

ALLOWED_PORTS = {80, 443, 8080, 8443}
MAX_REDIRECTS = 5
USER_AGENT = "ScholarBot/1.0 (+open-access article retrieval; legal sources only)"


class UnsafeURLError(ValueError):
    """Raised when a URL is refused for safety reasons."""


def is_public_ip(ip: str) -> bool:
    addr = ipaddress.ip_address(ip)
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return addr.is_global and not (addr.is_multicast or addr.is_loopback or addr.is_link_local
                                   or addr.is_private or addr.is_reserved or addr.is_unspecified)


async def resolve_host(host: str, port: int) -> list[str]:
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise UnsafeURLError(f"cannot resolve host: {host}") from exc
    return list(dict.fromkeys(info[4][0] for info in infos))


def check_url_syntax(url: str) -> tuple[str, str, int]:
    if not url or len(url) > 2048:
        raise UnsafeURLError("empty or too long URL")
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https"):
        raise UnsafeURLError("only http and https URLs are allowed")
    if parts.username or parts.password:
        raise UnsafeURLError("credentials in URL are not allowed")
    host = (parts.hostname or "").rstrip(".").lower()
    if not host or host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        raise UnsafeURLError("local hostnames are not allowed")
    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError as exc:
        raise UnsafeURLError("invalid port") from exc
    if port not in ALLOWED_PORTS:
        raise UnsafeURLError(f"port {port} is not allowed")
    return parts.scheme, host, port


async def validate_url(url: str, allow_private: bool = False) -> tuple[str, str, int, str]:
    """Return (scheme, host, port, pinned_ip) or raise UnsafeURLError."""
    scheme, host, port = check_url_syntax(url) if not allow_private else _relaxed(url)
    try:
        ipaddress.ip_address(host)
        ips = [host]
    except ValueError:
        ips = await resolve_host(host, port)
    if not ips:
        raise UnsafeURLError("host did not resolve")
    if not allow_private and not all(is_public_ip(ip) for ip in ips):
        raise UnsafeURLError("URL resolves to a non-public address")
    return scheme, host, port, ips[0]


def _relaxed(url: str) -> tuple[str, str, int]:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise UnsafeURLError("only http and https URLs are allowed")
    return parts.scheme, parts.hostname, parts.port or (443 if parts.scheme == "https" else 80)


@asynccontextmanager
async def safe_stream(url: str, *, headers: dict | None = None, timeout: float = 30.0,
                      allow_private: bool = False, transport: httpx.AsyncBaseTransport | None = None,
                      verify=True) -> AsyncIterator[tuple[httpx.Response, str]]:
    """GET `url` safely, following redirects manually. Yields (streaming response, final URL)."""
    current = url.strip()
    base_headers = {"User-Agent": USER_AGENT, "Accept": "*/*", **(headers or {})}
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False, transport=transport, verify=verify) as client:
        for _ in range(MAX_REDIRECTS + 1):
            scheme, host, port, ip = await validate_url(current, allow_private)
            default_port = 443 if scheme == "https" else 80
            host_header = host if port == default_port else f"{host}:{port}"
            ip_url_host = f"[{ip}]" if ":" in ip else ip
            parts = urlsplit(current)
            pinned = parts._replace(netloc=f"{ip_url_host}:{port}").geturl()
            req = client.build_request("GET", pinned, headers={**base_headers, "Host": host_header},
                                       extensions={"sni_hostname": host} if scheme == "https" else {})
            resp = await client.send(req, stream=True)
            location = resp.headers.get("location")
            if resp.is_redirect and location:
                await resp.aclose()
                current = urljoin(current, location)
                continue
            try:
                yield resp, current
            finally:
                await resp.aclose()
            return
        raise UnsafeURLError("too many redirects")


async def fetch_limited(url: str, max_bytes: int, **kw) -> tuple[bytes, str, str]:
    """Fetch up to `max_bytes` (error if exceeded). Returns (body, content_type, final_url)."""
    async with safe_stream(url, **kw) as (resp, final_url):
        if resp.status_code >= 400:
            raise httpx.HTTPStatusError(f"HTTP {resp.status_code}", request=resp.request, response=resp)
        buf = bytearray()
        async for chunk in resp.aiter_bytes():
            buf.extend(chunk)
            if len(buf) > max_bytes:
                raise UnsafeURLError("response too large")
        return bytes(buf), resp.headers.get("content-type", ""), final_url
