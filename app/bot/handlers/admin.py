"""In-bot administration. Every entry point re-checks that the *clicking/typing* Bale user is a configured admin
(IDs from ADMIN_BALE_IDS / Settings); callback data alone is never trusted."""
from __future__ import annotations

import logging
from types import SimpleNamespace

from sqlalchemy import func, select

from app.bot.handlers.common import BotContext, fmt_size, reply
from app.bot.keyboards import menus
from app.bot.middleware.admin import is_bot_admin
from app.core.localization import t
from app.database.database import session_scope
from app.database.models import AuditLog, DownloadJob, ErrorEvent, ProviderConfig, User, utcnow
from app.services import backup as backup_svc
from app.services import stats as dash_stats

log = logging.getLogger(__name__)


def _who(u: User) -> str:
    return (f"@{u.username}" if u.username else (u.first_name or "?")) + f" ({u.bale_id})"


def _ts(dt) -> str:
    return dt.strftime("%m-%d %H:%M") if dt else "—"


def _audit(session, admin_bale_id: int, action: str, target: str | None = None, detail: str | None = None) -> None:
    session.add(AuditLog(admin_username=f"bale:{admin_bale_id}", action=f"bot_{action}", target=target, detail=detail, ip="bale-bot"))


async def handle_command(ctx: BotContext, chat_id: int, lang: str, bale_id: int, cmd: str, args: list[str]) -> None:
    if not is_bot_admin(bale_id):
        # regular users get no hint that admin commands exist, except when they press the admin button / use /admin
        key = "err_not_admin" if cmd == "/admin" else "msg_unknown_command"
        return await reply(ctx, chat_id, t(lang, key), menus.main_menu(lang))
    if cmd == "/admin":
        return await show_panel(ctx, chat_id, lang)
    if cmd == "/stats":
        return await show_stats(ctx, chat_id, lang)
    if cmd in ("/block", "/unblock"):
        if len(args) != 1 or not args[0].isdigit():
            return await reply(ctx, chat_id, t(lang, "err_usage_block", cmd=cmd), menus.admin_back(lang))
        return await set_blocked(ctx, chat_id, lang, bale_id, args[0], cmd == "/block", by_bale_id=True)
    if cmd == "/quota":
        if len(args) != 2 or not args[0].isdigit() or not (args[1].isdigit() or args[1].lower() == "default"):
            return await reply(ctx, chat_id, t(lang, "err_usage_quota"), menus.admin_back(lang))
        return await set_quota(ctx, chat_id, lang, bale_id, int(args[0]), None if args[1].lower() == "default" else int(args[1]))


async def handle_callback(ctx: BotContext, chat_id: int, lang: str, bale_id: int, data: str) -> None:
    if not is_bot_admin(bale_id):
        log.warning("non-admin %s tried admin callback %r", bale_id, data[:30])
        return await reply(ctx, chat_id, t(lang, "err_not_admin"))
    action, *rest = data.split(":")
    if action == "home":
        return await show_panel(ctx, chat_id, lang)
    if action == "stats":
        return await show_stats(ctx, chat_id, lang)
    if action == "src":
        return await show_sources(ctx, chat_id, lang)
    if action == "tg" and rest:
        return await toggle_source(ctx, chat_id, lang, bale_id, rest[0])
    if action == "users":
        return await show_users(ctx, chat_id, lang)
    if action == "u" and rest and rest[0].isdigit():
        return await show_user(ctx, chat_id, lang, int(rest[0]))
    if action == "blk" and len(rest) == 2 and rest[0].isdigit():
        return await set_blocked(ctx, chat_id, lang, bale_id, rest[0], rest[1] == "1", by_bale_id=False)
    if action == "err":
        return await show_errors(ctx, chat_id, lang)
    if action == "req":
        return await show_requests(ctx, chat_id, lang)
    if action == "bak":
        return await make_backup(ctx, chat_id, lang, bale_id)
    if action == "cln":
        return await run_cleanup(ctx, chat_id, lang, bale_id)


async def show_panel(ctx: BotContext, chat_id: int, lang: str) -> None:
    await reply(ctx, chat_id, t(lang, "adm_title"), menus.admin_panel(lang))


async def show_stats(ctx: BotContext, chat_id: int, lang: str) -> None:
    runner = ctx.runner or SimpleNamespace(status={"state": "?"})
    async with session_scope() as s:
        st = await dash_stats.overview(s, SimpleNamespace(storage=ctx.storage, runner=runner, tasks=ctx.tasks))
        midnight = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        today = (await s.execute(select(func.count(DownloadJob.id)).where(DownloadJob.created_at >= midnight))).scalar_one()
    rate = f"{st['success_rate']}%" if st["success_rate"] is not None else "—"
    text = t(lang, "adm_stats_text", users=st["users_total"], active=st["users_active"], searches=st["searches"], today=today,
             rate=rate, errors=st["errors_24h"], queued=st["queued"], active_jobs=st["active"], bot=st["bot"]["state"],
             articles=st["articles_found"], saved=st["saved"], stored=fmt_size(st["stored_bytes"]) or "0", disk_free=fmt_size(st["disk_free"]))
    await reply(ctx, chat_id, text, menus.admin_back(lang))


async def show_sources(ctx: BotContext, chat_id: int, lang: str) -> None:
    pm = getattr(ctx.runner, "providers", None)
    if pm is None:
        return await reply(ctx, chat_id, t(lang, "err_internal"), menus.admin_back(lang))
    async with session_scope() as s:
        rows = {r.name: r for r in (await s.execute(select(ProviderConfig))).scalars()}
    lines, items = [t(lang, "adm_sources_title")], []
    for name, p in pm.providers.items():
        cfg, row = pm.config[name], rows.get(name)
        configured = p.is_configured()
        icon = "⚪" if not configured or row is None or row.last_status == "unknown" else ("✅" if row.last_status == "ok" else "❌")
        line = f"{icon} {name} · {t(lang, 'adm_priority')} {cfg['priority']} · {t(lang, 'adm_on' if cfg['enabled'] else 'adm_off')}"
        if row is not None:
            line += f" · {row.total_requests}/{row.total_errors}/{row.total_hits}"
        if not configured and p.config_hint():
            line += f"\n   ⚠ {p.config_hint()}"
        elif row is not None and row.last_status == "error" and row.last_error:
            line += f"\n   ⚠ {row.last_error[:90]}"
        lines.append(line)
        items.append((name, cfg["enabled"]))
    lines.append("\n" + t(lang, "adm_sources_help"))
    await reply(ctx, chat_id, "\n".join(lines), menus.admin_sources(lang, items))


async def toggle_source(ctx: BotContext, chat_id: int, lang: str, admin_id: int, name: str) -> None:
    pm = getattr(ctx.runner, "providers", None)
    if pm is None or name not in pm.providers:
        return await reply(ctx, chat_id, t(lang, "err_invalid_value"), menus.admin_back(lang))
    async with session_scope() as s:
        row = await s.get(ProviderConfig, name)
        if row is None:
            return await reply(ctx, chat_id, t(lang, "err_invalid_value"), menus.admin_back(lang))
        row.enabled = not row.enabled
        pm.config[name]["enabled"] = row.enabled
        _audit(s, admin_id, "source_toggled", name, f"enabled={row.enabled}")
    await show_sources(ctx, chat_id, lang)


async def show_users(ctx: BotContext, chat_id: int, lang: str) -> None:
    async with session_scope() as s:
        cnt = func.count(DownloadJob.id)
        rows = (await s.execute(select(User, cnt).outerjoin(DownloadJob, DownloadJob.user_id == User.id).group_by(User.id)
                                .order_by(User.last_seen_at.desc()).limit(10))).all()
    if not rows:
        return await reply(ctx, chat_id, t(lang, "adm_no_users"), menus.admin_back(lang))
    lines = [t(lang, "adm_users_title")]
    buttons = []
    for i, (u, n) in enumerate(rows, 1):
        lines.append(f"{i}. {_who(u)} · {n} {t(lang, 'adm_requests_word')}{' · ⛔' if u.is_blocked else ''} · {_ts(u.last_seen_at)}")
        buttons.append((u.id, _who(u) + (" ⛔" if u.is_blocked else "")))
    lines.append("\n" + t(lang, "adm_users_help"))
    await reply(ctx, chat_id, "\n".join(lines), menus.admin_users(buttons, lang))


async def show_user(ctx: BotContext, chat_id: int, lang: str, uid: int) -> None:
    async with session_scope() as s:
        u = await s.get(User, uid)
        if u is None:
            return await reply(ctx, chat_id, t(lang, "err_user_not_found"), menus.admin_back(lang))
        n = (await s.execute(select(func.count(DownloadJob.id)).where(DownloadJob.user_id == uid))).scalar_one()
    quota = u.quota_daily if u.quota_daily is not None else t(lang, "adm_default")
    text = t(lang, "adm_user_detail", who=_who(u), language=u.language or "—", joined=_ts(u.created_at), seen=_ts(u.last_seen_at),
             n=n, quota=quota, state=t(lang, "status_blocked" if u.is_blocked else "status_active"))
    await reply(ctx, chat_id, text, menus.admin_user_actions(lang, u.id, u.is_blocked))


async def set_blocked(ctx: BotContext, chat_id: int, lang: str, admin_id: int, ident: str, blocked: bool, *, by_bale_id: bool) -> None:
    async with session_scope() as s:
        if by_bale_id:
            u = (await s.execute(select(User).where(User.bale_id == int(ident)))).scalar_one_or_none()
        else:
            u = await s.get(User, int(ident))
        if u is None:
            return await reply(ctx, chat_id, t(lang, "err_user_not_found"), menus.admin_back(lang))
        if blocked and is_bot_admin(u.bale_id):
            return await reply(ctx, chat_id, t(lang, "err_admin_protect"), menus.admin_back(lang))
        u.is_blocked = blocked
        _audit(s, admin_id, "user_blocked" if blocked else "user_unblocked", f"bale:{u.bale_id}")
        uid = u.id
    await reply(ctx, chat_id, t(lang, "adm_user_blocked" if blocked else "adm_user_unblocked"))
    await show_user(ctx, chat_id, lang, uid)


async def set_quota(ctx: BotContext, chat_id: int, lang: str, admin_id: int, bale_id: int, quota: int | None) -> None:
    async with session_scope() as s:
        u = (await s.execute(select(User).where(User.bale_id == bale_id))).scalar_one_or_none()
        if u is None:
            return await reply(ctx, chat_id, t(lang, "err_user_not_found"), menus.admin_back(lang))
        u.quota_daily = quota
        _audit(s, admin_id, "user_quota_changed", f"bale:{bale_id}", "default" if quota is None else str(quota))
        uid = u.id
    await reply(ctx, chat_id, t(lang, "adm_quota_set", quota=t(lang, "adm_default") if quota is None else quota))
    await show_user(ctx, chat_id, lang, uid)


async def show_errors(ctx: BotContext, chat_id: int, lang: str) -> None:
    async with session_scope() as s:
        rows = (await s.execute(select(ErrorEvent).order_by(ErrorEvent.created_at.desc()).limit(10))).scalars().all()
    if not rows:
        return await reply(ctx, chat_id, t(lang, "adm_none_errors"), menus.admin_back(lang))
    lines = [t(lang, "adm_errors_title")] + [f"{_ts(e.created_at)} · {e.kind}/{e.source}: {e.message[:110]}" for e in rows]
    await reply(ctx, chat_id, "\n".join(lines), menus.admin_back(lang))


async def show_requests(ctx: BotContext, chat_id: int, lang: str) -> None:
    async with session_scope() as s:
        rows = (await s.execute(select(DownloadJob).order_by(DownloadJob.created_at.desc()).limit(10))).unique().scalars().all()
    if not rows:
        return await reply(ctx, chat_id, t(lang, "adm_none_requests"), menus.admin_back(lang))
    lines = [t(lang, "adm_requests_title")]
    for j in rows:
        who = _who(j.user) if j.user else "—"
        lines.append(f"{_ts(j.created_at)} · {who} · {j.query_type} · “{j.query_text[:40]}” → {t(lang, 'status_' + j.status)}"
                     + (f" ({j.error_code})" if j.error_code else ""))
    await reply(ctx, chat_id, "\n".join(lines), menus.admin_back(lang))


async def make_backup(ctx: BotContext, chat_id: int, lang: str, admin_id: int) -> None:
    if ctx.runner is None:
        return await reply(ctx, chat_id, t(lang, "err_internal"), menus.admin_back(lang))
    try:
        path = await backup_svc.create_backup(ctx.runner.s)
    except Exception:  # noqa: BLE001
        log.exception("bot-triggered backup failed")
        return await reply(ctx, chat_id, t(lang, "adm_backup_failed"), menus.admin_back(lang))
    async with session_scope() as s:
        _audit(s, admin_id, "backup_created", path.name)
    await reply(ctx, chat_id, t(lang, "adm_backup_done", name=path.name, size=fmt_size(path.stat().st_size)), menus.admin_back(lang))


async def run_cleanup(ctx: BotContext, chat_id: int, lang: str, admin_id: int) -> None:
    maintenance = getattr(ctx.runner, "maintenance", None)
    if maintenance is None:
        return await reply(ctx, chat_id, t(lang, "err_internal"), menus.admin_back(lang))
    res = await maintenance.run_cleanup()
    async with session_scope() as s:
        _audit(s, admin_id, "cleanup_run", None, str(res))
    await reply(ctx, chat_id, t(lang, "adm_cleanup_done", n=res["temp_files_removed"]), menus.admin_back(lang))
