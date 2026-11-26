"""Minimal async client for the official Bale Bot API (https://docs.bale.ai).

Only methods documented by Bale are used: getMe, getUpdates, sendMessage, sendDocument, editMessageText,
deleteMessage, plus a best-effort answerCallbackQuery / deleteWebhook (errors are tolerated).
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger(__name__)
MAX_TEXT = 4096
MAX_CAPTION = 1024
MAX_UPLOAD_BYTES = 50 * 1024 * 1024  # documented limit for multipart uploads


class BaleError(Exception):
    def __init__(self, description: str, code: int | None = None, retry_after: int | None = None):
        super().__init__(description)
        self.description, self.code, self.retry_after = description, code, retry_after


class BaleClient:
    def __init__(self, token: str, base_url: str = "https://tapi.bale.ai", transport: httpx.AsyncBaseTransport | None = None):
        self._base = f"{base_url.rstrip('/')}/bot{token}"
        self._http = httpx.AsyncClient(transport=transport, timeout=httpx.Timeout(30.0, read=90.0))

    async def aclose(self) -> None:
        await self._http.aclose()

    async def call(self, method: str, payload: dict[str, Any] | None = None, *, files: dict | None = None,
                   timeout: float | None = None) -> Any:
        url = f"{self._base}/{method}"
        kw: dict[str, Any] = {"timeout": timeout} if timeout else {}
        try:
            if files:
                data = {k: (json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else str(v))
                        for k, v in (payload or {}).items() if v is not None}
                resp = await self._http.post(url, data=data, files=files, **kw)
            else:
                resp = await self._http.post(url, json={k: v for k, v in (payload or {}).items() if v is not None}, **kw)
        except httpx.TimeoutException as exc:
            raise BaleError("timeout") from exc
        except httpx.HTTPError as exc:
            raise BaleError(f"network error: {type(exc).__name__}") from exc
        try:
            body = resp.json()
        except ValueError as exc:
            raise BaleError(f"invalid response (HTTP {resp.status_code})", resp.status_code) from exc
        if not body.get("ok"):
            params = body.get("parameters") or {}
            raise BaleError(str(body.get("description") or "unknown error"), body.get("error_code") or resp.status_code,
                            params.get("retry_after"))
        return body.get("result")

    # --- API methods -----------------------------------------------------------------------------
    async def get_me(self) -> dict:
        return await self.call("getMe")

    async def get_updates(self, offset: int | None, limit: int = 50, timeout: int = 0) -> list[dict]:
        payload: dict[str, Any] = {"offset": offset, "limit": limit}
        if timeout:
            payload["timeout"] = timeout  # accepted by Telegram-style servers; harmless if ignored
        return await self.call("getUpdates", payload, timeout=timeout + 30 if timeout else None) or []

    async def delete_webhook(self) -> None:
        try:
            await self.call("deleteWebhook")
        except BaleError as exc:
            log.info("deleteWebhook not applied: %s", exc.description)

    async def send_message(self, chat_id: int, text: str, reply_markup: dict | None = None) -> dict:
        return await self.call("sendMessage", {"chat_id": chat_id, "text": text[:MAX_TEXT], "reply_markup": reply_markup})

    async def edit_message_text(self, chat_id: int, message_id: int, text: str) -> dict:
        return await self.call("editMessageText", {"chat_id": chat_id, "message_id": message_id, "text": text[:MAX_TEXT]})

    async def delete_message(self, chat_id: int, message_id: int) -> None:
        try:
            await self.call("deleteMessage", {"chat_id": chat_id, "message_id": message_id})
        except BaleError:
            pass

    async def answer_callback(self, callback_id: str, text: str | None = None) -> None:
        try:
            await self.call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text})
        except BaleError:
            pass  # not documented by Bale for every client; the UI works without it

    async def send_document(self, chat_id: int, path: Path, filename: str, caption: str | None = None,
                            reply_markup: dict | None = None) -> dict:
        size = path.stat().st_size
        if size > MAX_UPLOAD_BYTES:
            raise BaleError("file exceeds Bale upload limit", 413)
        with path.open("rb") as fh:
            files = {"document": (filename, fh, "application/pdf")}
            return await self.call("sendDocument", {"chat_id": chat_id, "caption": (caption or "")[:MAX_CAPTION] or None,
                                                   "reply_markup": reply_markup}, files=files, timeout=300)


async def with_retry(factory, attempts: int = 3):
    """Retry transient failures (network, 429, 5xx) with backoff; honours retry_after."""
    for i in range(attempts):
        try:
            return await factory()
        except BaleError as exc:
            transient = exc.description in ("timeout",) or exc.description.startswith("network") or \
                (exc.code is not None and (exc.code == 429 or exc.code >= 500))
            if not transient or i == attempts - 1:
                raise
            await asyncio.sleep(min(exc.retry_after or 2 ** (i + 1), 15))
