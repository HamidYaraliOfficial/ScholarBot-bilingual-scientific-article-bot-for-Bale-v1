"""Every bot button and section, driven through the real poller exactly like a Bale user would (regular users and admins, fa + en).
Buttons are tapped using the callback data the bot itself put on its keyboards."""
from __future__ import annotations

import asyncio

import httpx
import pytest
from sqlalchemy import select

from app.bot import runner as runner_mod
from app.core.localization import t
from app.core.runtime_settings import runtime
from app.database.database import session_scope
from app.database.models import AuditLog, DownloadJob, ErrorEvent, ProviderConfig, User
from app.bot.middleware import rate_limit
from tests.helpers import CROSSREF_WORK, DOI, FakeBale, jresp

USER, ADMIN, ADMIN2 = 555, 777, 888
PANEL_BUTTONS = ["adm_btn_stats", "adm_btn_sources", "adm_btn_users", "adm_btn_errors", "adm_btn_requests", "adm_btn_backup", "adm_btn_cleanup"]


class Driver:
    """One Bale user talking to the bot."""

    def __init__(self, bale: FakeBale, uid: int, lang: str, username: str = "tester"):
        self.bale, self.uid, self.lang, self.username = bale, uid, lang, username

    # --- sending
    def say(self, text: str) -> int:
        mark = len(self.bale.calls)
        self.bale.push_message(text, user_id=self.uid, username=self.username)
        return mark

    def tap(self, data: str) -> int:
        mark = len(self.bale.calls)
        self.bale.push_callback(data, user_id=self.uid)
        return mark

    # --- reading
    def sent(self, since: int = 0) -> list[dict]:
        return [p for m, p in self.bale.calls[since:] if m == "sendMessage" and p.get("chat_id") == self.uid]

    def texts(self, since: int = 0) -> list[str]:
        return [p.get("text", "") for p in self.sent(since)]

    def docs(self) -> list[dict]:
        return [d for d in self.bale.documents if d["chat_id"] == self.uid]

    def inline(self, since: int = 0) -> list[dict]:
        """Buttons of the most recent message that carried an inline keyboard."""
        for p in reversed(self.sent(since)):
            kb = (p.get("reply_markup") or {}).get("inline_keyboard")
            if kb:
                return [b for row in kb for b in row]
        return []

    def keyboard_texts(self, since: int = 0) -> list[str]:
        for p in reversed(self.sent(since)):
            kb = (p.get("reply_markup") or {}).get("keyboard")
            if kb:
                return [b["text"] for row in kb for b in row]
        return []

    async def wait(self, pred, since: int, timeout: float = 20.0) -> bool:
        end = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < end:
            if pred(self.sent(since)):
                return True
            await asyncio.sleep(0.03)
        return False

    async def quiet(self, quiet: float = 0.2, max_wait: float = 15.0) -> None:
        """Wait until the bot has stopped sending messages to this chat, so a late message from an earlier action
        cannot be mistaken for the reply to the next one."""
        end = asyncio.get_event_loop().time() + max_wait
        last, since = len(self.sent()), asyncio.get_event_loop().time()
        while asyncio.get_event_loop().time() < end:
            await asyncio.sleep(0.05)
            now, n = asyncio.get_event_loop().time(), len(self.sent())
            if n != last:
                last, since = n, now
            elif now - since >= quiet:
                return

    async def says(self, text: str, expect: str | None = None, timeout: float = 20.0) -> list[dict]:
        """Send text and wait until the bot answered (optionally with an exact text)."""
        await self.quiet()
        mark = self.say(text)
        ok = await self.wait((lambda msgs: any(m.get("text") == expect for m in msgs)) if expect else (lambda msgs: bool(msgs)), mark, timeout)
        assert ok, f"no reply to {text!r}; bot sent {self.texts(mark)}"
        return self.sent(mark)

    async def taps(self, data: str, expect_pred=None, timeout: float = 20.0) -> list[dict]:
        await self.quiet()
        mark = self.tap(data)
        ok = await self.wait(expect_pred or (lambda msgs: bool(msgs)), mark, timeout)
        assert ok, f"no reply to tap {data!r}; bot sent {self.texts(mark)}"
        return self.sent(mark)

    async def tap_button(self, contains: str, since: int = 0, expect_pred=None) -> list[dict]:
        btn = next((b for b in self.inline(since) if contains in b["text"]), None)
        assert btn, f"no button containing {contains!r} in {[b['text'] for b in self.inline(since)]}"
        return await self.taps(btn["callback_data"], expect_pred)

    async def wait_docs(self, n: int, timeout: float = 25.0) -> bool:
        end = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < end:
            if len(self.docs()) >= n:
                return True
            await asyncio.sleep(0.03)
        return False

    async def join(self) -> None:
        """/start → language picker → tap the language button (as a new user must)."""
        msgs = await self.says("/start")
        assert msgs[-1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "lang:fa"
        await self.taps(f"lang:{self.lang}", lambda m: any(x.get("text") == t(self.lang, "msg_welcome") for x in m))


@pytest.fixture
async def bot(services, monkeypatch):
    monkeypatch.setattr(runner_mod, "EMPTY_POLL_MIN_SECONDS", 0.03)
    rate_limit._limiters.clear()
    runtime._v["admin_bale_id"] = f"{ADMIN}, {ADMIN2}"
    await services.tasks.start()
    await services.runner.start()
    bale: FakeBale = services.mocks["bale"]

    def make(uid: int, lang: str, username: str = "tester") -> Driver:
        return Driver(bale, uid, lang, username)

    async def settle(timeout: float = 25.0) -> None:
        """Wait until no job is queued/processing any more (so late bot messages cannot be mistaken for replies)."""
        end = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < end:
            async with session_scope() as s:
                busy = (await s.execute(select(DownloadJob).where(DownloadJob.status.in_(("queued", "processing"))))).unique().scalars().all()
            if not busy:
                await asyncio.sleep(0.15)
                return
            await asyncio.sleep(0.05)
        raise AssertionError("jobs did not finish")
    make.services, make.bale, make.settle = services, bale, settle  # type: ignore[attr-defined]
    return make


def ambiguous_titles(services):
    second = dict(CROSSREF_WORK["message"], DOI="10.1234/other", title=["Deep Learning for Open Science: A Survey"])
    services.mocks["providers"].on("api.crossref.org", lambda r: jresp({"message": {"items": [CROSSREF_WORK["message"], second]}})
                                   if r.url.path == "/works" else jresp(CROSSREF_WORK))


# =====================================================================================  regular user
@pytest.mark.parametrize("lang", ["fa", "en"])
async def test_every_menu_button_answers_for_a_regular_user(bot, lang):
    u = bot(USER, lang); await u.join()
    expected = {"btn_search": t(lang, "prompt_query"), "btn_doi": t(lang, "prompt_doi"), "btn_link": t(lang, "prompt_link"),
                "btn_author": t(lang, "prompt_author"), "btn_recent": t(lang, "msg_no_recent"), "btn_saved": t(lang, "msg_no_bookmarks"),
                "btn_help": t(lang, "msg_help")}
    for key, text in expected.items():
        msgs = await u.says(t(lang, key), text)
        assert msgs[-1]["text"] == text, key
    for key in ("btn_settings", "btn_language"):
        msgs = await u.says(t(lang, key))
        assert msgs[-1]["text"].startswith(t(lang, "msg_settings", language="", used=0, quota=0).split("\n")[0]), key
        assert [b["callback_data"] for b in u.inline()] == ["lang:fa", "lang:en"]
    kb = u.keyboard_texts()
    assert set(kb) == {t(lang, k) for k in ["btn_search", "btn_doi", "btn_link", "btn_author", "btn_recent", "btn_saved", "btn_help", "btn_settings", "btn_language"]}
    assert t(lang, "btn_admin") not in kb                                  # no admin entry for regular users
    assert "/myid" in t(lang, "msg_help") and t(lang, "msg_help_admin") not in (await u.says("/help"))[-1]["text"]


@pytest.mark.parametrize("lang", ["fa", "en"])
async def test_regular_user_cannot_reach_admin_functions(bot, lang):
    u = bot(USER, lang); await u.join()
    assert (await u.says(t(lang, "btn_admin")))[-1]["text"] == t(lang, "err_not_admin")
    assert (await u.says("/admin"))[-1]["text"] == t(lang, "err_not_admin")
    for cmd in ("/stats", "/block 1", "/unblock 1", "/quota 1 5"):
        assert (await u.says(cmd))[-1]["text"] == t(lang, "msg_unknown_command"), cmd
    for data in ("adm:stats", "adm:users", "adm:src", "adm:tg:crossref", "adm:blk:1:1", "adm:bak", "adm:cln", "adm:err", "adm:req", "adm:home"):
        msgs = await u.taps(data)
        assert msgs[-1]["text"] == t(lang, "err_not_admin"), data
    assert all("Users:" not in x and "کاربران:" not in x for x in u.texts())          # nothing leaked
    assert not any(m == "createBackup" for m, _ in bot.bale.calls)
    async with session_scope() as s:
        assert (await s.execute(select(AuditLog).where(AuditLog.action.like("bot_%")))).scalars().all() == []
    assert (await u.says("/myid"))[-1]["text"] == t(lang, "msg_myid", id=USER)


@pytest.mark.parametrize("lang", ["fa", "en"])
async def test_doi_button_flow_delivers_pdf(bot, lang):
    u = bot(USER, lang); await u.join()
    await u.says(t(lang, "btn_doi"), t(lang, "prompt_doi"))
    assert (await u.says("this is not a doi"))[-1]["text"] == t(lang, "err_invalid_doi")          # state kept: user may retry
    mark = u.say(DOI)
    assert await u.wait_docs(1)
    texts = u.texts(mark)
    assert t(lang, "msg_searching") in texts
    info = next(x for x in texts if x.startswith(t(lang, "msg_article_found")))
    assert "Deep Learning for Open Science" in info and DOI in info and t(lang, "msg_pdf_next") in info
    assert u.docs()[0]["has_pdf"] and u.docs()[0]["filename"] == "10.1234_abc.pdf"
    assert await u.wait(lambda m: any(x.get("text") == t(lang, "msg_done") for x in m), mark)
    async with session_scope() as s:
        assert (await s.execute(select(User).where(User.bale_id == USER))).scalar_one().state is None


@pytest.mark.parametrize("lang", ["fa", "en"])
async def test_link_button_flow(bot, lang):
    u = bot(USER, lang); await u.join()
    await u.says(t(lang, "btn_link"), t(lang, "prompt_link"))
    assert (await u.says("just some words"))[-1]["text"] == t(lang, "err_invalid_link")
    u.say(f"https://publisher.example.com/doi/{DOI}")
    assert await u.wait_docs(1)


@pytest.mark.parametrize("lang", ["fa", "en"])
async def test_title_search_button_exact_and_ambiguous(bot, lang):
    u = bot(USER, lang); await u.join()
    await u.says(t(lang, "btn_search"), t(lang, "prompt_query"))
    u.say("Deep Learning for Open Science")                       # exact title: resolved without asking
    assert await u.wait_docs(1)
    ambiguous_titles(bot.services)
    await u.says(t(lang, "btn_search"), t(lang, "prompt_query"))
    mark = u.say("deep learning open")
    assert await u.wait(lambda m: any(x.get("text", "").startswith(t(lang, "msg_choose_article")) for x in m), mark)
    buttons = u.inline(mark)
    assert len(buttons) == 2 and all(b["callback_data"].startswith("pick:") for b in buttons)
    await u.tap_button("Survey", mark)                              # choose the second candidate by its button
    assert await u.wait_docs(2)
    assert "Survey" in " ".join(u.texts(mark))


@pytest.mark.parametrize("lang", ["fa", "en"])
async def test_author_button_flow(bot, lang):
    u = bot(USER, lang); await u.join()
    assert (await u.says(t(lang, "btn_author"), t(lang, "prompt_author")))
    assert (await u.says("ab"))[-1]["text"] == t(lang, "err_query_too_short")
    mark = u.say("Ada Lovelace")
    assert await u.wait(lambda m: any(x.get("text", "").startswith(t(lang, "msg_choose_article")) for x in m), mark)
    await u.tap_button("Deep Learning", mark)
    assert await u.wait_docs(1)


async def test_recent_and_saved_lists_resend_and_bookmarks(bot):
    u = bot(USER, "en"); await u.join()
    mark = u.say(DOI)
    assert await u.wait_docs(1)
    # bookmark button on the article message
    msgs = await u.tap_button("Save to my list", mark, lambda m: any(x.get("text") == t("en", "msg_bookmarked") for x in m))
    assert any(b.get("url", "").endswith(DOI) for b in u.inline(mark))                  # official-page URL button is present
    mark = u.say(t("en", "btn_recent"))
    assert await u.wait(lambda m: any(x.get("text") == t("en", "msg_recent") for x in m), mark)
    await u.tap_button("Deep Learning", mark)                                           # re-delivery from the recent list
    assert await u.wait_docs(2)
    mark = u.say(t("en", "btn_saved"))
    assert await u.wait(lambda m: any(x.get("text") == t("en", "msg_bookmarks") for x in m), mark)
    await u.tap_button("Deep Learning", mark)                                           # ... and from the saved list
    assert await u.wait_docs(3)
    assert bot.services.mocks["downloads"].count("repo.example.org") == 1               # never downloaded twice
    mark = u.say(DOI)
    assert await u.wait_docs(4)
    await u.tap_button("Remove from my list", mark, lambda m: any(x.get("text") == t("en", "msg_unbookmarked") for x in m))
    assert (await u.says(t("en", "btn_saved")))[-1]["text"] == t("en", "msg_no_bookmarks")


async def test_language_switching_and_button_text_variants(bot):
    u = bot(USER, "en"); await u.join()
    await u.says(t("en", "btn_settings"))
    msgs = await u.taps("lang:fa", lambda m: any(x.get("text") == t("fa", "msg_welcome") for x in m))
    assert t("fa", "btn_doi") in u.keyboard_texts()
    # a Persian-language user can still press a button that carries the English label (and vice versa)
    assert (await u.says(t("en", "btn_doi"), t("fa", "prompt_doi")))
    # emoji / variation selectors / ZWNJ differences must not break button matching
    assert (await u.says("⚙ تنظیمات"))[-1]["text"].startswith("⚙️ تنظیمات")
    assert (await u.says("تنظیمات"))[-1]["text"].startswith("⚙️ تنظیمات")
    assert (await u.says("جست وجوی مقاله", t("fa", "prompt_query")))
    await u.taps("lang:en", lambda m: any(x.get("text") == t("en", "msg_welcome") for x in m))
    assert (await u.says("settings"))[-1]["text"].startswith("⚙️ Settings")
    assert (await u.says("🔎 search articles", t("en", "prompt_query")))
    assert (await u.says("/cancel", t("en", "msg_cancelled")))


async def test_quota_ratelimit_and_blocked_user_through_the_bot(bot):
    runtime._v["user_daily_quota"] = 2
    u = bot(USER, "en"); await u.join()
    await u.says("10.1000/q1"); await u.says("10.1000/q2"); await bot.settle()
    assert (await u.says("10.1000/q3"))[-1]["text"] == t("en", "err_quota", limit=2)
    async with session_scope() as s:
        usr = (await s.execute(select(User).where(User.bale_id == USER))).scalar_one(); usr.is_blocked = True
    assert (await u.says("hello"))[-1]["text"] == t("en", "err_blocked")
    assert (await u.taps("lang:fa"))[-1]["text"] == t("en", "err_blocked")


# =====================================================================================  administrators
@pytest.mark.parametrize("lang", ["fa", "en"])
async def test_admin_sees_panel_button_and_every_panel_section_works(bot, lang):
    # a regular user has already used the bot (so the user list is not empty)
    reg = bot(USER, lang, "regular"); await reg.join(); await reg.says(DOI); assert await reg.wait_docs(1)
    a = bot(ADMIN, lang, "boss"); await a.join()
    assert t(lang, "btn_admin") in a.keyboard_texts()
    msgs = await a.says(t(lang, "btn_admin"), t(lang, "adm_title"))
    assert [b["text"] for b in a.inline()] == [t(lang, k) for k in PANEL_BUTTONS]
    assert t(lang, "msg_help_admin") in (await a.says("/help"))[-1]["text"]
    # statistics
    st = (await a.taps("adm:stats"))[-1]["text"]
    assert st.startswith(t(lang, "adm_stats_text", users=0, active=0, searches=0, today=0, rate="", errors=0, queued=0, active_jobs=0, bot="", articles=0, saved=0, stored="", disk_free="").split("\n")[0])
    assert "2" in st and "running" in st
    # requests + errors
    async with session_scope() as s:
        s.add(ErrorEvent(kind="provider", source="crossref", message="boom-from-test"))
    assert "boom-from-test" in (await a.taps("adm:err"))[-1]["text"]
    rq = (await a.taps("adm:req"))[-1]["text"]
    assert DOI in rq and "@regular" in rq
    # users: list -> detail -> block -> blocked user is shut out -> unblock -> works again
    users_msg = await a.taps("adm:users")
    assert "@regular" in users_msg[-1]["text"] and "@boss" in users_msg[-1]["text"]
    detail = await a.tap_button("regular", 0)
    assert "@regular" in detail[-1]["text"]
    await a.tap_button(t(lang, "adm_btn_block"), 0, lambda m: any(x.get("text", "").startswith("👤") and t(lang, "status_blocked") in x["text"] for x in m))
    assert (await reg.says("hello"))[-1]["text"] == t(lang, "err_blocked")
    await a.tap_button(t(lang, "adm_btn_unblock"), 0, lambda m: any(x.get("text", "").startswith("👤") and t(lang, "status_active") in x["text"] for x in m))
    assert (await reg.says(t(lang, "btn_help")))[-1]["text"] == t(lang, "msg_help")
    # backup + cleanup
    bak = (await a.taps("adm:bak"))[-1]["text"]
    files = list(bot.services.settings.backup_dir.glob("scholarbot_backup_*.zip"))
    assert len(files) == 1 and files[0].name in bak
    assert (await a.taps("adm:cln"))[-1]["text"] == t(lang, "adm_cleanup_done", n=0)
    assert (await a.taps("adm:home"))[-1]["text"] == t(lang, "adm_title")
    async with session_scope() as s:
        actions = {x.action for x in (await s.execute(select(AuditLog))).scalars()}
    assert {"bot_user_blocked", "bot_user_unblocked", "bot_backup_created", "bot_cleanup_run"} <= actions


async def test_admin_sources_toggle_changes_real_behaviour(bot):
    a = bot(ADMIN, "en"); await a.join()
    msgs = await a.taps("adm:src")
    listing = msgs[-1]["text"]
    for name in ("crossref", "openalex", "unpaywall", "europe_pmc", "doaj", "arxiv", "core"):
        assert name in listing
    assert "CONTACT_EMAIL" not in listing and "CORE_API_KEY" in listing              # unconfigured source explains what is missing
    pm = bot.services.providers
    assert pm.config["unpaywall"]["enabled"]
    await a.tap_button("unpaywall", 0)
    assert pm.config["unpaywall"]["enabled"] is False
    async with session_scope() as s:
        assert (await s.get(ProviderConfig, "unpaywall")).enabled is False
    assert "⛔ unpaywall" in [b["text"] for b in a.inline()]
    # with Unpaywall off the PDF can no longer be found (it was the only source of OA links in this setup)
    u = bot(USER, "en"); await u.join()
    mark = u.say(DOI)
    assert await u.wait(lambda m: any(x.get("text", "").startswith(t("en", "msg_article_found_nopdf")) for x in m), mark)
    await a.tap_button("unpaywall", 0)
    assert pm.config["unpaywall"]["enabled"] is True
    assert (await a.taps("adm:tg:nonexistent"))[-1]["text"] == t("en", "err_invalid_value")


async def test_admin_commands_block_quota_and_protection(bot):
    reg = bot(USER, "en", "regular"); await reg.join()
    a = bot(ADMIN, "en", "boss"); await a.join()
    assert (await a.says("/block"))[-1]["text"] == t("en", "err_usage_block", cmd="/block")
    assert (await a.says("/block abc"))[-1]["text"] == t("en", "err_usage_block", cmd="/block")
    assert (await a.says("/block 99999"))[-1]["text"] == t("en", "err_user_not_found")
    assert (await a.says(f"/block {ADMIN2}"))[-1]["text"] == t("en", "err_user_not_found")        # unknown until they used the bot
    boss2 = bot(ADMIN2, "en", "boss2"); await boss2.join()
    assert (await a.says(f"/block {ADMIN2}"))[-1]["text"] == t("en", "err_admin_protect")     # admins cannot lock each other out
    assert (await a.says(f"/block {ADMIN}"))[-1]["text"] == t("en", "err_admin_protect")
    mark = a.say(f"/block {USER}")
    assert await a.wait(lambda m: any(x.get("text") == t("en", "adm_user_blocked") for x in m), mark)
    assert await a.wait(lambda m: any(x.get("text", "").startswith("👤") and t("en", "status_blocked") in x["text"] for x in m), mark)
    assert (await reg.says("hi"))[-1]["text"] == t("en", "err_blocked")
    mark = a.say(f"/unblock {USER}")
    assert await a.wait(lambda m: any(x.get("text") == t("en", "adm_user_unblocked") for x in m), mark)
    assert (await reg.says(t("en", "btn_help")))[-1]["text"] == t("en", "msg_help")
    assert (await a.says("/quota 555"))[-1]["text"] == t("en", "err_usage_quota")
    assert (await a.says("/quota 555 many"))[-1]["text"] == t("en", "err_usage_quota")
    assert (await a.says("/quota 99999 5"))[-1]["text"] == t("en", "err_user_not_found")
    mark = a.say(f"/quota {USER} 1")
    assert await a.wait(lambda m: any(x.get("text") == t("en", "adm_quota_set", quota=1) for x in m), mark)
    await reg.says("10.1000/one"); await bot.settle()
    assert (await reg.says("10.1000/two"))[-1]["text"] == t("en", "err_quota", limit=1)
    mark = a.say(f"/quota {USER} default")
    assert await a.wait(lambda m: any(x.get("text") == t("en", "adm_quota_set", quota=t("en", "adm_default")) for x in m), mark)
    async with session_scope() as s:
        assert (await s.execute(select(User).where(User.bale_id == USER))).scalar_one().quota_daily is None
    mark = a.say("/stats")
    assert await a.wait(lambda m: any("Users:" in x.get("text", "") for x in m), mark)


async def test_admin_is_also_a_normal_user_and_multiple_ids(bot):
    for uid in (ADMIN, ADMIN2):                                    # "777, 888" in the setting: both are admins
        a = bot(uid, "fa", f"adm{uid}"); await a.join()
        assert t("fa", "btn_admin") in a.keyboard_texts()
        await a.says(t("fa", "btn_doi"), t("fa", "prompt_doi"))
        mark = a.say(DOI)
        assert await a.wait_docs(1)
        assert t("fa", "btn_admin") in a.keyboard_texts(mark)      # menu after a delivery still has the admin button
    runtime._v["admin_bale_id"] = ""                               # removing the setting revokes admin rights immediately
    a = bot(ADMIN, "fa", "adm777")
    assert (await a.says("/admin"))[-1]["text"] == t("fa", "err_not_admin")
    assert (await a.taps("adm:stats"))[-1]["text"] == t("fa", "err_not_admin")


async def test_admin_ids_from_env_setting_and_validation(bot, settings):
    runtime._v["admin_bale_id"] = ""
    settings.admin_bale_ids = "4242"
    a = bot(4242, "en", "envadmin"); await a.join()
    assert (await a.says("/admin"))[-1]["text"] == t("en", "adm_title")
    from app.core.runtime_settings import coerce, DEFINITIONS
    for good in ("123", "123,456", "123 456", "123; 456", ""):
        coerce(DEFINITIONS["admin_bale_id"], good)
    for bad in ("abc", "12,ab", "-5", "1.5"):
        with pytest.raises(ValueError):
            coerce(DEFINITIONS["admin_bale_id"], bad)
