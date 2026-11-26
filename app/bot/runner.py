"""Long-polling runner + the Notifier that delivers pipeline results to users in Bale."""
from __future__ import annotations

import asyncio
import logging
import time

import httpx
from sqlalchemy import select

from app.bot.bale_client import BaleClient, BaleError, with_retry
from app.bot.handlers.callbacks import handle_callback
from app.bot.handlers.common import BotContext, article_info, official_links, reply
from app.bot.handlers.messages import handle_message
from app.bot.keyboards import menus
from app.bot.middleware.admin import is_bot_admin
from app.core.config import Settings
from app.core.localization import normalize_lang, t
from app.core.security import safe_filename
from app.database.database import session_scope
from app.database.models import Article, ArticleFile, Bookmark, DownloadJob, ErrorEvent
from app.providers.base import ArticleMeta
from app.services.pipeline import DeliveryReport
from app.services.library import Library
from app.services.storage_manager import StorageManager
from app.services.task_manager import TaskManager

log = logging.getLogger(__name__)
POLL_TIMEOUT = 20
EMPTY_POLL_MIN_SECONDS = 1.0  # if the server answers an empty poll immediately, wait this long (no hot loop)
REASON_KEYS = {"no_legal_pdf", "only_publisher_page", "too_large", "file_corrupt", "download_failed"}


class BotRunner:
    def __init__(self, settings: Settings, tasks: TaskManager, library: Library, storage: StorageManager,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.s, self.tasks, self.library, self.storage, self.transport = settings, tasks, library, storage, transport
        self.client: BaleClient | None = None
        self.providers = None      # set by the service container (ProviderManager)
        self.maintenance = None    # set by the service container (Maintenance)
        self.ctx: BotContext | None = None
        self._task: asyncio.Task | None = None
        self.status = {"state": "disabled", "username": None, "last_error": None, "last_poll": None, "handled": 0}
        self._last_logged: dict[str, float] = {}

    # --- lifecycle ----------------------------------------------------------------------------
    async def start(self) -> None:
        await self.stop()
        token = self.s.bale_bot_token.strip()
        if not token:
            self.status.update(state="disabled", username=None, last_error=None)
            log.warning("BALE_BOT_TOKEN is not set: dashboard runs, bot is disabled")
            return
        self.client = BaleClient(token, self.s.bale_api_base, self.transport)
        self.ctx = BotContext(self.client, self.tasks, self.library, self.storage, runner=self)
        self.status.update(state="connecting", last_error=None)
        self._task = asyncio.create_task(self._run(), name="scholarbot-bale-poller")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        if self.client:
            await self.client.aclose()
        self.client = self.ctx = None

    async def _note_error(self, message: str) -> None:
        self.status["last_error"] = message
        now = time.monotonic()
        if now - self._last_logged.get(message, 0) > 60:
            self._last_logged[message] = now
            log.error("bale: %s", message)
            try:
                async with session_scope() as s:
                    s.add(ErrorEvent(kind="bot", source="bale", message=message[:1000]))
            except Exception:  # noqa: BLE001
                pass

    async def _run(self) -> None:
        assert self.client
        backoff = 2
        offset: int | None = None
        connected = False
        while True:
            try:
                if not connected:
                    me = await self.client.get_me()
                    await self.client.delete_webhook()
                    self.status.update(state="running", username=me.get("username"), last_error=None)
                    connected, backoff = True, 2
                started = time.monotonic()
                updates = await self.client.get_updates(offset, 50, POLL_TIMEOUT)
                self.status["last_poll"] = time.time()
                for u in updates:
                    offset = max(offset or 0, u["update_id"] + 1)
                    await self._dispatch(u)
                if not updates and time.monotonic() - started < EMPTY_POLL_MIN_SECONDS:
                    await asyncio.sleep(EMPTY_POLL_MIN_SECONDS)  # server answered immediately: avoid a hot loop
                backoff = 2
            except asyncio.CancelledError:
                raise
            except BaleError as exc:
                connected = False if exc.code in (401, 403, 404) or not connected else connected
                self.status["state"] = "error"
                await self._note_error(f"{exc.description} (code {exc.code})" if exc.code else exc.description)
                await asyncio.sleep(min(exc.retry_after or backoff, 60))
                backoff = min(backoff * 2, 60)
            except Exception as exc:  # noqa: BLE001
                self.status["state"] = "error"
                await self._note_error(f"unexpected: {type(exc).__name__}")
                log.exception("poller crashed; retrying")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)
            else:
                if self.status["state"] == "error" and connected:
                    self.status["state"] = "running"

    async def _dispatch(self, update: dict) -> None:
        assert self.ctx
        try:
            if "message" in update:
                await handle_message(self.ctx, update["message"])
            elif "callback_query" in update:
                await handle_callback(self.ctx, update["callback_query"])
            self.status["handled"] += 1
        except Exception:  # noqa: BLE001 - one bad update must not stop the bot
            log.exception("update %s failed", update.get("update_id"))
            await self._note_error("handler error")


class BotNotifier:
    """Implements the pipeline's Notifier on top of the running bot."""

    def __init__(self, runner: BotRunner):
        self.r = runner

    def _ctx(self) -> BotContext | None:
        return self.r.ctx

    async def _drop_status(self, ctx: BotContext, job: DownloadJob) -> None:
        if job.status_message_id and job.chat_id:
            await ctx.client.delete_message(job.chat_id, job.status_message_id)

    async def progress(self, job: DownloadJob, stage: str) -> None:
        ctx = self._ctx()
        if ctx and stage == "downloading" and job.status_message_id and job.chat_id:
            try:
                await ctx.client.edit_message_text(job.chat_id, job.status_message_id, t(normalize_lang(job.lang), "msg_downloading"))
            except BaleError:
                pass

    async def candidates(self, job: DownloadJob, metas: list[ArticleMeta]) -> None:
        ctx = self._ctx()
        if not ctx or not job.chat_id:
            return
        lang = normalize_lang(job.lang)
        await self._drop_status(ctx, job)
        lines = [t(lang, "msg_choose_article")]
        for i, m in enumerate(metas):
            by = ", ".join(m.authors[:3]) + (" …" if len(m.authors) > 3 else "")
            extra = " — ".join(x for x in (by, str(m.year) if m.year else "", m.journal or "") if x)
            lines.append(f"{i + 1}. {m.title}\n   {extra}" if extra else f"{i + 1}. {m.title}")
        await reply(ctx, job.chat_id, "\n\n".join(lines), menus.candidates_keyboard(job.id, metas))

    async def failure(self, job: DownloadJob, code: str) -> None:
        ctx = self._ctx()
        if not ctx or not job.chat_id:
            return
        lang = normalize_lang(job.lang)
        await self._drop_status(ctx, job)
        key = f"err_{code}"
        text = t(lang, key)
        await reply(ctx, job.chat_id, text if text != key else t(lang, "err_internal"), menus.main_menu(lang, is_bot_admin(job.chat_id)))

    async def article(self, job: DownloadJob, article: Article, file: ArticleFile | None,
                      reason: str | None) -> DeliveryReport | None:
        ctx = self._ctx()
        if not ctx or not job.chat_id:
            return DeliveryReport(False, error="bot offline") if file else None
        lang, chat = normalize_lang(job.lang), job.chat_id
        await self._drop_status(ctx, job)
        async with session_scope() as s:
            bookmarked = bool((await s.execute(select(Bookmark.id).where(Bookmark.user_id == job.user_id,
                                                                         Bookmark.article_id == article.id))).first())
        markup = menus.article_actions(lang, article.id, bookmarked, article.doi_url or article.landing_url)
        if file is None:
            key = reason if reason in REASON_KEYS else "download_failed"
            links = official_links(article)
            footer = t(lang, f"err_{key}") + (f"\n\n{t(lang, 'lbl_official_links')}:\n{links}" if links else "")
            await reply(ctx, chat, article_info(lang, article, None, "msg_article_found_nopdf", footer), markup)
            await reply(ctx, chat, t(lang, "msg_menu"), menus.main_menu(lang, is_bot_admin(chat)))
            return None
        await reply(ctx, chat, article_info(lang, article, file, footer=t(lang, "msg_pdf_next")), markup)
        path = self.r.storage.abs_path(file.path, file.is_permanent)
        caption = "\n".join(x for x in (article.title, f"DOI: {article.doi}" if article.doi else "") if x)
        try:
            sent = await with_retry(lambda: ctx.client.send_document(
                chat, path, f"{safe_filename(article.doi or article.title, 80)}.pdf", caption))
        except (BaleError, OSError) as exc:
            msg = exc.description if isinstance(exc, BaleError) else "file unreadable"
            links = official_links(article)
            await reply(ctx, chat, t(lang, "err_send_failed") + (f"\n\n{links}" if links else ""), menus.main_menu(lang, is_bot_admin(chat)))
            return DeliveryReport(False, error=msg)
        await reply(ctx, chat, t(lang, "msg_done"), menus.main_menu(lang, is_bot_admin(chat)))
        return DeliveryReport(True, message_id=(sent or {}).get("message_id"))
