"""Text message handling: commands, menu buttons and article requests."""
from __future__ import annotations

import logging
import unicodedata

from sqlalchemy import func, select

from app.bot.handlers import admin as admin_panel
from app.bot.handlers.common import BotContext, reply
from app.bot.keyboards import menus
from app.bot.middleware.admin import is_bot_admin
from app.bot.middleware.rate_limit import check_user_rate
from app.bot.middleware.user_context import load_user
from app.core.localization import all_labels, t
from app.database.database import session_scope
from app.database.models import Article, Bookmark, DownloadJob, User
from app.database.repositories.users import daily_quota, jobs_in_last_day
from app.services.parsing import InvalidInput, parse_input

log = logging.getLogger(__name__)
STATE_BUTTONS = {"btn_search": ("await_query", "prompt_query"), "btn_doi": ("await_doi", "prompt_doi"),
                 "btn_link": ("await_link", "prompt_link"), "btn_author": ("await_author", "prompt_author")}
STATE_HINTS = {"await_query": None, "await_doi": "doi", "await_link": "link", "await_author": "author"}
BUTTON_KEYS = ["btn_search", "btn_doi", "btn_link", "btn_author", "btn_recent", "btn_saved", "btn_help", "btn_settings",
               "btn_language", "btn_admin"]


def norm_label(text: str) -> str:
    """Compare button texts by their words only: emoji, variation selectors, ZWNJ and punctuation are ignored, so a
    button still matches if a client sends the emoji slightly differently (e.g. '⚙ ' instead of '⚙️ ')."""
    text = unicodedata.normalize("NFKC", text or "").replace("ي", "ی").replace("ك", "ک")
    return " ".join("".join(ch if ch.isalnum() else " " for ch in text).lower().split())


def _label_map() -> dict[str, str]:
    return {norm_label(label): key for key in BUTTON_KEYS for label in all_labels(key) if norm_label(label)}


def menu(lang: str, bale_id: int | None) -> dict:
    return menus.main_menu(lang, is_bot_admin(bale_id))


async def set_state(user_id: int, state: str | None) -> None:
    async with session_scope() as s:
        user = await s.get(User, user_id)
        if user:
            user.state = state


async def send_menu(ctx: BotContext, chat_id: int, lang: str, text_key: str = "msg_menu") -> None:
    await reply(ctx, chat_id, t(lang, text_key), menu(lang, chat_id))


async def send_help(ctx: BotContext, chat_id: int, lang: str) -> None:
    text = t(lang, "msg_help") + (("\n\n" + t(lang, "msg_help_admin")) if is_bot_admin(chat_id) else "")
    await reply(ctx, chat_id, text, menu(lang, chat_id))


async def handle_message(ctx: BotContext, msg: dict) -> None:
    chat, frm = msg.get("chat") or {}, msg.get("from")
    if not frm or frm.get("is_bot") or chat.get("type", "private") != "private":
        return
    chat_id = chat["id"]
    text = (msg.get("text") or "").strip()
    async with session_scope() as s:
        user, lang = await load_user(s, frm)
        uid, bale_id, blocked, has_lang, state = user.id, user.bale_id, user.is_blocked, user.language is not None, user.state
    if blocked:
        await reply(ctx, chat_id, t(lang, "err_blocked"))
        return
    if not has_lang:
        await reply(ctx, chat_id, t("fa", "msg_choose_language") + "\n\n" + t("en", "msg_choose_language"), menus.language_picker())
        return
    if not text:
        await reply(ctx, chat_id, t(lang, "err_text_only"))
        return

    if text.startswith("/"):
        parts = text.split()
        cmd = parts[0].lower().split("@")[0]
        if cmd == "/start":
            await set_state(uid, None)
            return await send_menu(ctx, chat_id, lang, "msg_welcome")
        if cmd in ("/menu", "/cancel"):
            await set_state(uid, None)
            return await send_menu(ctx, chat_id, lang, "msg_cancelled" if cmd == "/cancel" else "msg_menu")
        if cmd == "/help":
            return await send_help(ctx, chat_id, lang)
        if cmd == "/myid":
            return await reply(ctx, chat_id, t(lang, "msg_myid", id=bale_id), menu(lang, bale_id))
        if cmd in ("/language", "/settings"):
            return await show_settings(ctx, chat_id, uid, lang)
        if cmd in ("/admin", "/stats", "/block", "/unblock", "/quota"):
            return await admin_panel.handle_command(ctx, chat_id, lang, bale_id, cmd, parts[1:])
        if cmd in ("/search", "/doi", "/author", "/link"):
            key = {"/search": "btn_search", "/doi": "btn_doi", "/author": "btn_author", "/link": "btn_link"}[cmd]
            return await start_flow(ctx, chat_id, uid, lang, key)
        return await reply(ctx, chat_id, t(lang, "msg_unknown_command"), menu(lang, bale_id))

    key = _label_map().get(norm_label(text))
    if key in STATE_BUTTONS:
        return await start_flow(ctx, chat_id, uid, lang, key)
    if key == "btn_recent":
        return await show_recent(ctx, chat_id, uid, lang)
    if key == "btn_saved":
        return await show_bookmarks(ctx, chat_id, uid, lang)
    if key == "btn_help":
        return await send_help(ctx, chat_id, lang)
    if key in ("btn_settings", "btn_language"):
        return await show_settings(ctx, chat_id, uid, lang)
    if key == "btn_admin":
        return await admin_panel.handle_command(ctx, chat_id, lang, bale_id, "/admin", [])
    await submit_query(ctx, uid, bale_id, chat_id, lang, text, STATE_HINTS.get(state or ""))


async def start_flow(ctx: BotContext, chat_id: int, uid: int, lang: str, key: str) -> None:
    state, prompt = STATE_BUTTONS[key]
    await set_state(uid, state)
    await reply(ctx, chat_id, t(lang, prompt), menu(lang, chat_id))


async def submit_query(ctx: BotContext, uid: int, bale_id: int, chat_id: int, lang: str, text: str, hint: str | None) -> None:
    allowed, wait = check_user_rate(bale_id)
    if not allowed:
        return await reply(ctx, chat_id, t(lang, "err_rate_limited", seconds=wait))
    try:
        parsed = parse_input(text, hint)
    except InvalidInput as exc:
        return await reply(ctx, chat_id, t(lang, f"err_{exc.code}"), menu(lang, bale_id))
    async with session_scope() as s:
        user = await s.get(User, uid)
        quota = daily_quota(user)
        if await jobs_in_last_day(s, uid) >= quota:
            return await reply(ctx, chat_id, t(lang, "err_quota", limit=quota))
        dup = (await s.execute(select(func.count()).select_from(DownloadJob).where(
            DownloadJob.user_id == uid, DownloadJob.query_text == text, DownloadJob.status.in_(("queued", "processing"))))).scalar_one()
        if dup:
            return await reply(ctx, chat_id, t(lang, "msg_already_processing"))
        user.state = None
        job = DownloadJob(user_id=uid, chat_id=chat_id, lang=lang, query_text=text, query_type=parsed.kind, status="queued")
        s.add(job)
        await s.flush()
        job_id = job.id
    await enqueue(ctx, job_id, chat_id, lang)


async def enqueue(ctx: BotContext, job_id: int, chat_id: int, lang: str) -> None:
    sent = await reply(ctx, chat_id, t(lang, "msg_searching"))
    if sent:
        async with session_scope() as s:
            job = await s.get(DownloadJob, job_id)
            job.status_message_id = sent.get("message_id")
    await ctx.tasks.submit(job_id)


async def show_settings(ctx: BotContext, chat_id: int, uid: int, lang: str) -> None:
    async with session_scope() as s:
        user = await s.get(User, uid)
        used, quota = await jobs_in_last_day(s, uid), daily_quota(user)
    name = {"fa": "فارسی", "en": "English"}.get(lang, lang)
    await reply(ctx, chat_id, t(lang, "msg_settings", language=name, used=used, quota=quota), menus.settings_keyboard(lang))


async def show_recent(ctx: BotContext, chat_id: int, uid: int, lang: str) -> None:
    async with session_scope() as s:
        rows = (await s.execute(select(DownloadJob).where(DownloadJob.user_id == uid, DownloadJob.article_id.is_not(None))
                                .order_by(DownloadJob.created_at.desc()).limit(40))).unique().scalars().all()
        seen, items = set(), []
        for j in rows:
            if j.article and j.article.status != "deleted" and j.article_id not in seen:
                seen.add(j.article_id)
                items.append((j.article_id, j.article.title or j.article.doi or "?"))
            if len(items) >= 10:
                break
    if not items:
        return await reply(ctx, chat_id, t(lang, "msg_no_recent"), menu(lang, chat_id))
    await reply(ctx, chat_id, t(lang, "msg_recent"), menus.article_list(items))


async def show_bookmarks(ctx: BotContext, chat_id: int, uid: int, lang: str) -> None:
    async with session_scope() as s:
        rows = (await s.execute(select(Article).join(Bookmark, Bookmark.article_id == Article.id).where(
            Bookmark.user_id == uid).order_by(Bookmark.created_at.desc()).limit(20))).unique().scalars().all()
        items = [(a.id, a.title or a.doi or "?") for a in rows]
    if not items:
        return await reply(ctx, chat_id, t(lang, "msg_no_bookmarks"), menu(lang, chat_id))
    await reply(ctx, chat_id, t(lang, "msg_bookmarks"), menus.article_list(items))
