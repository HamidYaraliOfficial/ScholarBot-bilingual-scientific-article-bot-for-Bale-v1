"""Inline-button callbacks."""
from __future__ import annotations

from sqlalchemy import delete, select

from app.bot.handlers import admin as admin_panel
from app.bot.handlers.common import BotContext, meta_from_article, reply
from app.bot.handlers.messages import enqueue, send_menu
from app.bot.middleware.user_context import load_user
from app.core.localization import normalize_lang, t
from app.database.database import session_scope
from app.database.models import Article, Bookmark, DownloadJob, User


async def handle_callback(ctx: BotContext, cq: dict) -> None:
    frm, data = cq.get("from"), cq.get("data") or ""
    chat_id = ((cq.get("message") or {}).get("chat") or {}).get("id") or (frm or {}).get("id")
    if not frm or not chat_id:
        return
    await ctx.client.answer_callback(cq.get("id", ""))
    async with session_scope() as s:
        user, lang = await load_user(s, frm)
        uid, blocked = user.id, user.is_blocked
    if blocked:
        return await reply(ctx, chat_id, t(lang, "err_blocked"))
    action, _, rest = data.partition(":")

    if action == "lang":
        new = normalize_lang(rest)
        async with session_scope() as s:
            (await s.get(User, uid)).language = new
        return await send_menu(ctx, chat_id, new, "msg_welcome")
    if action == "adm":
        return await admin_panel.handle_callback(ctx, chat_id, lang, int(frm["id"]), rest)
    if action == "pick":
        return await pick(ctx, uid, chat_id, lang, rest)
    if action == "bm":
        return await toggle_bookmark(ctx, uid, chat_id, lang, rest)
    if action == "re":
        return await resend(ctx, uid, chat_id, lang, rest)


async def pick(ctx: BotContext, uid: int, chat_id: int, lang: str, rest: str) -> None:
    try:
        job_id, idx = (int(x) for x in rest.split(":"))
    except ValueError:
        return
    async with session_scope() as s:
        job = await s.get(DownloadJob, job_id)
        cands = (job.candidates or []) if job and job.user_id == uid else []
        if not 0 <= idx < len(cands):
            return await reply(ctx, chat_id, t(lang, "err_choice_expired"))
        chosen = cands[idx]
        new = DownloadJob(user_id=uid, chat_id=chat_id, lang=lang, query_text=chosen.get("title", "")[:500],
                          query_type="pick", status="queued", candidates=[chosen])
        s.add(new)
        await s.flush()
        new_id = new.id
    await enqueue(ctx, new_id, chat_id, lang)


async def resend(ctx: BotContext, uid: int, chat_id: int, lang: str, rest: str) -> None:
    if not rest.isdigit():
        return
    async with session_scope() as s:
        art = await s.get(Article, int(rest))
        if art is None or art.status == "deleted":
            return await reply(ctx, chat_id, t(lang, "err_not_found"))
        job = DownloadJob(user_id=uid, chat_id=chat_id, lang=lang, query_text=(art.title or art.doi or "")[:500],
                          query_type="pick", status="queued", candidates=[meta_from_article(art)])
        s.add(job)
        await s.flush()
        job_id = job.id
    await enqueue(ctx, job_id, chat_id, lang)


async def toggle_bookmark(ctx: BotContext, uid: int, chat_id: int, lang: str, rest: str) -> None:
    if not rest.isdigit():
        return
    aid = int(rest)
    async with session_scope() as s:
        existing = (await s.execute(select(Bookmark).where(Bookmark.user_id == uid, Bookmark.article_id == aid))).scalar_one_or_none()
        if existing:
            await s.execute(delete(Bookmark).where(Bookmark.id == existing.id))
            key = "msg_unbookmarked"
        elif await s.get(Article, aid) is not None:
            s.add(Bookmark(user_id=uid, article_id=aid))
            key = "msg_bookmarked"
        else:
            return
    await reply(ctx, chat_id, t(lang, key))
