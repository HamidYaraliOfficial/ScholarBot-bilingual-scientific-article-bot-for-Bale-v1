"""Reply / inline keyboards in the structures documented by the Bale Bot API."""
from __future__ import annotations

from app.core.localization import t
from app.providers.base import ArticleMeta

MENU_KEYS = [["btn_search", "btn_doi"], ["btn_link", "btn_author"], ["btn_recent", "btn_saved"],
             ["btn_help", "btn_settings"], ["btn_language"]]


def main_menu(lang: str, admin: bool = False) -> dict:
    rows = [[{"text": t(lang, k)} for k in row] for row in MENU_KEYS]
    if admin:
        rows.append([{"text": t(lang, "btn_admin")}])
    return {"keyboard": rows}


def admin_panel(lang: str) -> dict:
    def b(key: str, data: str) -> dict:
        return {"text": t(lang, key), "callback_data": data}
    return {"inline_keyboard": [[b("adm_btn_stats", "adm:stats"), b("adm_btn_sources", "adm:src")],
                                [b("adm_btn_users", "adm:users"), b("adm_btn_errors", "adm:err")],
                                [b("adm_btn_requests", "adm:req"), b("adm_btn_backup", "adm:bak")],
                                [b("adm_btn_cleanup", "adm:cln")]]}


def admin_back(lang: str) -> dict:
    return {"inline_keyboard": [[{"text": t(lang, "adm_btn_back"), "callback_data": "adm:home"}]]}


def admin_users(users: list[tuple[int, str]], lang: str) -> dict:
    rows = [[{"text": f"{i + 1}. {_short(label, 40)}", "callback_data": f"adm:u:{uid}"}] for i, (uid, label) in enumerate(users)]
    rows.append([{"text": t(lang, "adm_btn_back"), "callback_data": "adm:home"}])
    return {"inline_keyboard": rows}


def admin_user_actions(lang: str, user_id: int, blocked: bool) -> dict:
    key, flag = ("adm_btn_unblock", 0) if blocked else ("adm_btn_block", 1)
    return {"inline_keyboard": [[{"text": t(lang, key), "callback_data": f"adm:blk:{user_id}:{flag}"}],
                                [{"text": t(lang, "adm_btn_users"), "callback_data": "adm:users"}]]}


def admin_sources(lang: str, items: list[tuple[str, bool]]) -> dict:
    rows = [[{"text": f"{'✅' if on else '⛔'} {name}", "callback_data": f"adm:tg:{name}"}] for name, on in items]
    rows.append([{"text": t(lang, "adm_btn_back"), "callback_data": "adm:home"}])
    return {"inline_keyboard": rows}


def language_picker() -> dict:
    return {"inline_keyboard": [[{"text": "🇮🇷 فارسی", "callback_data": "lang:fa"},
                                 {"text": "🇬🇧 English", "callback_data": "lang:en"}]]}


def _short(text: str, n: int = 48) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def candidates_keyboard(job_id: int, metas: list[ArticleMeta]) -> dict:
    rows = []
    for i, m in enumerate(metas):
        label = f"{i + 1}. {_short(m.title)}" + (f" ({m.year})" if m.year else "")
        rows.append([{"text": label, "callback_data": f"pick:{job_id}:{i}"}])
    return {"inline_keyboard": rows}


def article_actions(lang: str, article_id: int, bookmarked: bool, url: str | None) -> dict:
    row = [{"text": t(lang, "btn_unbookmark" if bookmarked else "btn_bookmark"), "callback_data": f"bm:{article_id}"}]
    rows = [row]
    if url and url.startswith(("http://", "https://")):
        rows.append([{"text": t(lang, "btn_official_page"), "url": url}])
    return {"inline_keyboard": rows}


def article_list(items: list[tuple[int, str]]) -> dict:
    return {"inline_keyboard": [[{"text": f"{i + 1}. {_short(title)}", "callback_data": f"re:{aid}"}]
                                for i, (aid, title) in enumerate(items)]}


def settings_keyboard(lang: str) -> dict:
    return language_picker()
