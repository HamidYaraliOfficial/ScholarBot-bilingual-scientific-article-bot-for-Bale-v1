import asyncio
import json

import httpx
import pytest
from sqlalchemy import select

from app.bot import runner as runner_mod
from app.bot.bale_client import BaleClient, BaleError, with_retry
from app.bot.handlers.callbacks import handle_callback
from app.bot.handlers.common import BotContext
from app.bot.handlers.messages import handle_message
from app.bot.middleware import rate_limit
from app.core.localization import t
from app.core.runtime_settings import runtime
from app.database.database import session_scope
from app.database.models import Article, Bookmark, DownloadJob, User
from tests.helpers import DOI, FakeBale, HostMock, jresp


async def eventually(cond, timeout=20.0):
    end = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < end:
        if await cond() if asyncio.iscoroutinefunction(cond) else cond():
            return True
        await asyncio.sleep(0.05)
    return False


@pytest.fixture(autouse=True)
def _clear_limiters(monkeypatch):
    rate_limit._limiters.clear()
    monkeypatch.setattr(runner_mod, "EMPTY_POLL_MIN_SECONDS", 0.05)


@pytest.fixture
def ctx(services):
    bale = services.mocks["bale"]
    return BotContext(BaleClient("123:TOKEN", "https://tapi.bale.ai", bale.transport()), services.tasks, services.library, services.storage)


async def say(ctx, text, uid=555, **kw):
    await handle_message(ctx, {"message_id": 1, "chat": {"id": uid, "type": "private"}, "text": text,
                               "from": {"id": uid, "is_bot": False, "first_name": "Alice", "username": "alice"}})


async def set_lang(uid=555, lang="en", **fields):
    async with session_scope() as s:
        u = (await s.execute(select(User).where(User.bale_id == uid))).scalar_one_or_none()
        if u is None:
            u = User(bale_id=uid); s.add(u)
        u.language = lang
        for k, v in fields.items():
            setattr(u, k, v)


# ---------------------------------------------------------------- API client
async def test_client_uses_documented_endpoints_and_maps_errors():
    seen = []

    def handle(req: httpx.Request):
        seen.append(req)
        if req.url.path.endswith("/getMe"):
            return jresp({"ok": True, "result": {"id": 1, "username": "b"}})
        if req.url.path.endswith("/sendMessage"):
            return jresp({"ok": False, "error_code": 429, "description": "Too Many Requests", "parameters": {"retry_after": 3}}, 429)
        return jresp({"ok": True, "result": {"message_id": 5}})
    c = BaleClient("42:ABC", "https://tapi.bale.ai", httpx.MockTransport(handle))
    assert (await c.get_me())["username"] == "b" and str(seen[0].url) == "https://tapi.bale.ai/bot42:ABC/getMe"
    with pytest.raises(BaleError) as e:
        await c.send_message(1, "x")
    assert e.value.code == 429 and e.value.retry_after == 3
    assert await c.call("deleteMessage", {"chat_id": 1, "message_id": 2}) == {"message_id": 5}


async def test_send_document_multipart_and_limits(tmp_path):
    from tests.helpers import make_pdf
    bale = FakeBale(); c = BaleClient("1:T", transport=bale.transport())
    f = tmp_path / "a.pdf"; f.write_bytes(make_pdf())
    res = await c.send_document(9, f, "paper.pdf", caption="x" * 2000, reply_markup={"inline_keyboard": []})
    assert res["message_id"] and bale.documents[0]["filename"] == "paper.pdf" and bale.documents[0]["has_pdf"]
    assert len(bale.documents[0]["caption"]) == 1024
    big = tmp_path / "big.pdf"; big.write_bytes(b"0")
    import app.bot.bale_client as bc
    bc.MAX_UPLOAD_BYTES, old = 0, bc.MAX_UPLOAD_BYTES
    try:
        with pytest.raises(BaleError):
            await c.send_document(9, big, "big.pdf")
    finally:
        bc.MAX_UPLOAD_BYTES = old


async def test_with_retry_only_retries_transient(monkeypatch):
    async def nosleep(*a): return None
    monkeypatch.setattr("app.bot.bale_client.asyncio.sleep", nosleep)
    n = {"c": 0}

    async def flaky():
        n["c"] += 1
        if n["c"] < 3:
            raise BaleError("Internal", 500)
        return "ok"
    assert await with_retry(flaky) == "ok" and n["c"] == 3

    async def permanent():
        n["c"] += 1
        raise BaleError("Bad Request", 400)
    n["c"] = 0
    with pytest.raises(BaleError):
        await with_retry(permanent)
    assert n["c"] == 1


# ---------------------------------------------------------------- conversation logic
async def test_first_contact_requires_language_choice_and_persists_it(services, ctx):
    bale = services.mocks["bale"]
    await say(ctx, "/start")
    kind, payload = bale.calls[-1]
    assert kind == "sendMessage" and payload["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "lang:fa"
    await handle_callback(ctx, {"id": "1", "data": "lang:en", "from": {"id": 555, "first_name": "Alice"}, "message": {"chat": {"id": 555}}})
    async with session_scope() as s:
        assert (await s.execute(select(User))).scalar_one().language == "en"
    last = bale.calls[-1][1]
    assert last["text"] == t("en", "msg_welcome") and last["reply_markup"]["keyboard"][0][0]["text"] == t("en", "btn_search")
    # a later session keeps English, and Persian text input still resolves menu buttons in either language
    await say(ctx, t("fa", "btn_help"))
    assert bale.sent_texts()[-1] == t("en", "msg_help")


async def test_menu_buttons_set_states_and_prompts(services, ctx):
    bale = services.mocks["bale"]; await set_lang(lang="fa")
    for key, prompt in [("btn_doi", "prompt_doi"), ("btn_link", "prompt_link"), ("btn_author", "prompt_author"), ("btn_search", "prompt_query")]:
        await say(ctx, t("fa", key))
        assert bale.sent_texts()[-1] == t("fa", prompt)
    async with session_scope() as s:
        assert (await s.execute(select(User))).scalar_one().state == "await_query"
    await say(ctx, "/cancel")
    assert bale.sent_texts()[-1] == t("fa", "msg_cancelled")


async def test_invalid_input_quota_ratelimit_and_blocking(services, ctx):
    bale = services.mocks["bale"]; await set_lang(lang="en")
    await say(ctx, t("en", "btn_doi")); await say(ctx, "definitely not a doi")
    assert bale.sent_texts()[-1] == t("en", "err_invalid_doi")
    runtime._v["user_rate_per_minute"] = 2
    for i in range(3):
        await say(ctx, f"10.1000/test{i}")
    assert "Too many requests" in bale.sent_texts()[-1]
    rate_limit._limiters.clear(); runtime._v["user_rate_per_minute"] = 60
    async with session_scope() as s:
        (await s.execute(select(User))).scalar_one().quota_daily = 2     # already used 2 jobs above
    await say(ctx, "10.1000/another")
    assert bale.sent_texts()[-1] == t("en", "err_quota", limit=2)
    await set_lang(lang="en", is_blocked=True)
    await say(ctx, "10.1000/blocked")
    assert bale.sent_texts()[-1] == t("en", "err_blocked")


async def test_duplicate_in_flight_request_is_rejected(services, ctx):
    bale = services.mocks["bale"]; await set_lang(lang="en")
    await say(ctx, "10.1000/same"); await say(ctx, "10.1000/same")
    assert bale.sent_texts()[-1] == t("en", "msg_already_processing")
    async with session_scope() as s:
        assert len((await s.execute(select(DownloadJob))).scalars().all()) == 1


async def test_groups_and_bots_are_ignored(services, ctx):
    bale = services.mocks["bale"]
    await handle_message(ctx, {"chat": {"id": -5, "type": "group"}, "text": "/start", "from": {"id": 1, "is_bot": False}})
    await handle_message(ctx, {"chat": {"id": 7, "type": "private"}, "text": "hi", "from": {"id": 7, "is_bot": True}})
    assert bale.calls == []


async def test_bookmarks_recent_and_admin_stats(services, ctx):
    bale = services.mocks["bale"]; await set_lang(lang="en")
    await say(ctx, t("en", "btn_saved")); assert bale.sent_texts()[-1] == t("en", "msg_no_bookmarks")
    await say(ctx, t("en", "btn_recent")); assert bale.sent_texts()[-1] == t("en", "msg_no_recent")
    async with session_scope() as s:
        u = (await s.execute(select(User))).scalar_one()
        a = Article(title="Saved One", doi="10.1/s", status="sent", authors=[], keywords=[], metadata_sources=[]); s.add(a); await s.flush()
        s.add(DownloadJob(user_id=u.id, query_text="x", query_type="doi", status="sent", article_id=a.id)); aid = a.id
    await say(ctx, t("en", "btn_recent"))
    assert bale.calls[-1][1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == f"re:{aid}"
    cb = {"id": "c", "from": {"id": 555, "first_name": "A"}, "message": {"chat": {"id": 555}}}
    await handle_callback(ctx, {**cb, "data": f"bm:{aid}"}); assert bale.sent_texts()[-1] == t("en", "msg_bookmarked")
    await say(ctx, t("en", "btn_saved")); assert bale.calls[-1][1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == f"re:{aid}"
    await handle_callback(ctx, {**cb, "data": f"bm:{aid}"}); assert bale.sent_texts()[-1] == t("en", "msg_unbookmarked")
    async with session_scope() as s:
        assert (await s.execute(select(Bookmark))).scalars().all() == []
    await say(ctx, "/stats"); assert bale.sent_texts()[-1] == t("en", "msg_unknown_command")   # not the configured admin
    runtime._v["admin_bale_id"] = "555"
    await say(ctx, "/stats"); assert "Users:" in bale.sent_texts()[-1]


# ---------------------------------------------------------------- end to end through the real poller
async def test_end_to_end_doi_request_delivers_pdf(services):
    bale: FakeBale = services.mocks["bale"]
    await services.tasks.start(); await services.runner.start()
    bale.push_message("/start")
    assert await eventually(lambda: any(m == "sendMessage" and "inline_keyboard" in (p.get("reply_markup") or {}) for m, p in bale.calls))
    bale.push_callback("lang:en")
    assert await eventually(lambda: any(p.get("text") == t("en", "msg_welcome") for _, p in bale.calls))
    bale.push_message(f"https://doi.org/{DOI}")
    assert await eventually(lambda: bool(bale.documents)), bale.sent_texts()
    texts = bale.sent_texts()
    assert t("en", "msg_searching") in texts
    info = next(x for x in texts if x.startswith(t("en", "msg_article_found")))
    assert "Title: Deep Learning for Open Science" in info and f"DOI: {DOI}" in info and "Authors: Ada Lovelace, Alan Turing" in info
    assert "Access Status: Open Access" in info and t("en", "msg_pdf_next") in info and "File Size:" in info
    doc = bale.documents[0]
    assert doc["has_pdf"] and doc["filename"] == "10.1234_abc.pdf" and DOI in doc["caption"]
    assert await eventually(lambda: t("en", "msg_done") in bale.sent_texts())
    assert services.runner.status["state"] == "running" and services.runner.status["username"] == "scholar_test_bot"
    # the job row is finalised right after the last message was sent, so poll for it instead of racing it
    async def job_sent() -> bool:
        async with session_scope() as s:
            return (await s.execute(select(DownloadJob))).scalars().one().status == "sent"
    assert await eventually(job_sent, timeout=15)


async def test_end_to_end_persian_and_no_pdf_fallback(services):
    from tests.helpers import UNPAYWALL_CLOSED
    services.mocks["providers"].on("api.unpaywall.org", lambda r: jresp(UNPAYWALL_CLOSED))
    bale: FakeBale = services.mocks["bale"]
    await set_lang(lang="fa")
    await services.tasks.start(); await services.runner.start()
    bale.push_message(DOI)
    assert await eventually(lambda: any(t("fa", "msg_article_found_nopdf") in x for x in bale.sent_texts()))
    msg = next(x for x in bale.sent_texts() if t("fa", "msg_article_found_nopdf") in x)
    assert t("fa", "err_no_legal_pdf") in msg and f"https://doi.org/{DOI}" in msg and bale.documents == []


async def test_poller_survives_api_errors_and_reports_state(services):
    bale: FakeBale = services.mocks["bale"]
    bale.fail["getMe"] = "Unauthorized"
    await services.runner.start()
    assert await eventually(lambda: services.runner.status["state"] == "error")
    assert "Unauthorized" in (services.runner.status["last_error"] or "")
    del bale.fail["getMe"]
    assert await eventually(lambda: services.runner.status["state"] == "running", timeout=12)


async def test_runner_without_token_is_disabled(services):
    services.settings.bale_bot_token = ""
    await services.runner.start()
    assert services.runner.status["state"] == "disabled" and services.runner.client is None
