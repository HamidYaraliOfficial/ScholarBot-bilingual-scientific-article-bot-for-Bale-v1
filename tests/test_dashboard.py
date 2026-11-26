import io
import json
import re

import httpx
import pytest
from openpyxl import load_workbook
from sqlalchemy import select

from app.core.security import hash_password
from app.database.database import session_scope
from app.database.models import Admin, Article, AuditLog, DownloadJob, SavedArticle, User
from app.database.repositories.articles import upsert_from_meta
from app.providers.base import ArticleMeta, OALink
from app.services.pdf_downloader import DownloadResult
from app.services.pdf_validator import sha256_file
from tests.conftest import ADMIN_PW, csrf_of, login
from tests.helpers import make_pdf


async def seed_articles(services, n=3, with_files=True):
    ids = []
    async with session_scope() as s:
        user = User(bale_id=900, username="reader", language="en"); s.add(user); await s.flush()
        for i in range(n):
            meta = ArticleMeta(title=f"Paper {i} on Graphs", doi=f"10.1/p{i}", authors=["Grace Hopper", f"Author {i}"], year=2020 + i, journal="J. Graphs")
            art = await upsert_from_meta(s, meta, user)
            if with_files:
                part = services.storage.part_path(); data = make_pdf(1, extra=f"p{i}".encode()); part.write_bytes(data)
                await services.library.attach_file(s, art, DownloadResult(part, sha256_file(part), len(data), 1, OALink("https://x/y.pdf", "unpaywall"), "https://x/y.pdf"))
                art.status = "sent"
            ids.append(art.id)
    return ids


def post(client, url, token, **data):
    return client.post(url, data=data, headers={"X-CSRF-Token": token})


async def test_authentication_required_everywhere(client):
    for path in ["/", "/articles", "/library", "/history", "/users", "/sources", "/admins", "/reports", "/settings", "/logs",
                 "/articles/1", "/articles/1/file", "/api/stats/charts", "/dashboard/cards", "/backups/x.zip", "/reports/errors.csv"]:
        r = await client.get(path)
        assert r.status_code == 303 and r.headers["location"] == "/login", path
    r = await client.get("/articles/table", headers={"HX-Request": "true"})
    assert r.status_code == 401 and r.headers["HX-Redirect"] == "/login"
    assert (await client.get("/api/health")).json()["database"] == "ok"            # health probe is public & harmless


async def test_login_logout_lockout_and_security_headers(client):
    tok = await csrf_of(client)
    bad = await client.post("/login", data={"username": "root", "password": "wrong", "csrf_token": tok})
    assert bad.status_code == 401
    assert (await client.post("/login", data={"username": "ghost", "password": "x", "csrf_token": tok})).status_code == 401
    assert (await client.post("/login", data={"username": "root", "password": ADMIN_PW})).status_code == 403   # no CSRF token
    csrf = await login(client)
    r = await client.get("/")
    assert r.status_code == 200 and "ScholarBot" in r.text
    h = r.headers
    assert "script-src 'self'" in h["content-security-policy"] and h["x-frame-options"] == "DENY" and h["x-content-type-options"] == "nosniff"
    assert (await post(client, "/logout", csrf)).status_code == 303
    assert (await client.get("/")).status_code == 303
    # five wrong passwords lock the account for a while, even with the right password afterwards
    for _ in range(5):
        tok = await csrf_of(client)
        await client.post("/login", data={"username": "root", "password": "nope", "csrf_token": tok})
    tok = await csrf_of(client)
    r = await client.post("/login", data={"username": "root", "password": ADMIN_PW, "csrf_token": tok})
    assert r.status_code == 429


async def test_csrf_is_enforced_on_state_changing_requests(client, services):
    csrf = await login(client)
    ids = await seed_articles(services, 1)
    assert (await client.post("/articles/bulk", data={"action": "save", "ids": ids})).status_code == 403
    assert (await client.post("/articles/bulk", data={"action": "save", "ids": ids}, headers={"X-CSRF-Token": "forged"})).status_code == 403
    r = await client.post("/articles/bulk", data={"action": "save", "ids": ids}, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200 and r.json()["ok"]


async def test_articles_page_filters_sorting_and_pagination(client, services):
    await login(client)
    await seed_articles(services, 5)
    r = await client.get("/articles")
    assert r.status_code == 200 and "Paper 0 on Graphs" in r.text and 'data-total="5"' in r.text
    r = await client.get("/articles/table?q=paper 3")
    assert "Paper 3 on Graphs" in r.text and "Paper 0 on Graphs" not in r.text and 'data-total="1"' in r.text
    assert 'data-total="5"' in (await client.get("/articles/table?author=hopper")).text
    assert 'data-total="1"' in (await client.get("/articles/table?year=2022")).text
    assert 'data-total="5"' in (await client.get("/articles/table?doi=10.1/p")).text
    assert 'data-total="0"' in (await client.get("/articles/table?status=failed")).text
    r = await client.get("/articles/table?sort=year&dir=asc&per_page=10")
    assert r.text.index("Paper 0") < r.text.index("Paper 4")
    r = await client.get("/articles/table?sort=size&dir=desc")
    assert r.status_code == 200
    r = await client.get("/articles/table?per_page=10&page=1&sort=title; DROP TABLE articles")   # unknown sort falls back safely
    assert r.status_code == 200
    # SQL-ish input is treated as data
    assert 'data-total="0"' in (await client.get("/articles/table?q=' OR 1=1 --")).text


async def test_bulk_save_unsave_delete_flow_with_confirmation(client, services):
    csrf = await login(client)
    ids = await seed_articles(services, 3)
    h = {"X-CSRF-Token": csrf}
    r = await client.post("/articles/bulk", data={"action": "save", "ids": ids[:2]}, headers=h)
    assert r.json()["ok"]
    async with session_scope() as s:
        assert len((await s.execute(select(SavedArticle))).scalars().all()) == 2
        files = [a.file for a in (await s.execute(select(Article))).unique().scalars() if a.saved]
        assert all(f.is_permanent and services.storage.exists(f.path, True) for f in files)
    lib = await client.get("/library")
    assert 'data-total="2"' in lib.text and "Paper 2" not in lib.text
    assert 'data-total="1"' in (await client.get("/articles/table?saved=temporary")).text
    # deletion needs explicit confirmation
    r = await client.post("/articles/bulk", data={"action": "delete", "ids": ids}, headers=h)
    assert r.status_code == 400 and not r.json()["ok"]
    r = await client.post("/articles/bulk", data={"action": "delete", "ids": ids[2:], "confirm": "yes", "mode": "info_and_file"}, headers=h)
    assert r.json()["ok"]
    async with session_scope() as s:
        a = (await s.get(Article, ids[2]))
        assert a.status == "deleted" and a.file is None
    assert 'data-total="2"' in (await client.get("/articles")).text.replace("data-total=\"2\"", 'data-total="2"')
    assert 'data-total="1"' in (await client.get("/articles/table?status=deleted")).text
    # select-all-filtered applies the filter server side
    r = await client.post("/articles/bulk", data={"action": "unsave", "all_filtered": "1", "library": "1"}, headers=h)
    assert r.json()["ok"] and "2" in r.json()["message"]
    async with session_scope() as s:
        assert (await s.execute(select(SavedArticle))).scalars().all() == []
        audits = [a.action for a in (await s.execute(select(AuditLog))).scalars()]
    assert {"articles_saved", "articles_deleted", "articles_unsaved", "login"} <= set(audits)
    assert (await client.post("/articles/bulk", data={"action": "save"}, headers=h)).status_code == 400


async def test_viewer_role_is_read_only(client, services):
    async with session_scope() as s:
        s.add(Admin(username="watcher", password_hash=hash_password("Viewer-Pass-77"), role="viewer"))
    ids = await seed_articles(services, 1)
    csrf = await login(client, "watcher", "Viewer-Pass-77")
    h = {"X-CSRF-Token": csrf}
    assert (await client.get("/articles")).status_code == 200 and (await client.get("/history")).status_code == 200
    assert 'data-bulk="delete"' not in (await client.get("/articles")).text
    for url, data in [("/articles/bulk", {"action": "save", "ids": ids}), ("/users/1/block", {"block": 1}), ("/settings", {"concurrency": 5}),
                      ("/backups/create", {}), ("/sources/crossref/update", {"priority": 5}), ("/admins/create", {"username": "zzz", "password": "Abcdefghij1!"}),
                      ("/sources/key", {"env_name": "CORE_API_KEY", "value": "k"}), ("/settings/cleanup", {})]:
        r = await client.post(url, data=data, headers=h)
        assert r.status_code == 403, url
    assert (await client.get("/admins")).status_code == 403 and (await client.get("/logs")).status_code == 403
    assert (await client.post("/articles/export", data={"fmt": "csv", "ids": ids}, headers=h)).status_code == 200   # exporting is allowed


async def test_exports_csv_xlsx_bibtex_ris_and_formula_injection(client, services):
    csrf = await login(client)
    ids = await seed_articles(services, 2, with_files=False)
    async with session_scope() as s:
        evil = (await upsert_from_meta(s, ArticleMeta(title='=HYPERLINK("http://evil","x")', doi="10.1/evil", authors=["+cmd|' /C calc'!A0"], year=2024), None))
        ids.append(evil.id)
    h = {"X-CSRF-Token": csrf}
    r = await client.post("/articles/export", data={"fmt": "csv", "ids": ids}, headers=h)
    text = r.content.decode("utf-8-sig")
    assert r.headers["content-disposition"].endswith('.csv"') and "Paper 0 on Graphs" in text
    assert "'=HYPERLINK" in text and ",=HYPERLINK" not in text and "'+cmd" in text
    r = await client.post("/articles/export", data={"fmt": "xlsx", "ids": ids}, headers=h)
    ws = load_workbook(io.BytesIO(r.content)).active
    cells = {str(c.value) for row in ws.iter_rows() for c in row if c.value is not None}
    assert "Paper 1 on Graphs" in cells and not any(v.startswith("=") for v in cells) and ws.max_row == 4
    r = await client.post("/articles/export", data={"fmt": "bibtex", "ids": ids[:1]}, headers=h)
    assert "@article{hopper2020paper" in r.text and "doi = {10.1/p0}" in r.text
    r = await client.post("/articles/export", data={"fmt": "ris", "ids": ids[:1]}, headers=h)
    assert "TY  - JOUR" in r.text and "DO  - 10.1/p0" in r.text
    assert (await client.post("/articles/export", data={"fmt": "pdf", "ids": ids}, headers=h)).status_code == 400
    assert (await client.post("/articles/export", data={"fmt": "csv"}, headers=h)).status_code == 400


async def test_zip_file_download_and_detail_partial(client, services):
    import zipfile
    csrf = await login(client)
    ids = await seed_articles(services, 2)
    r = await client.post("/articles/zip", data={"ids": ids}, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200 and sorted(zipfile.ZipFile(io.BytesIO(r.content)).namelist()) == ["10.1_p0.pdf", "10.1_p1.pdf"]
    assert list(services.settings.export_dir.glob("*.zip")) == []      # the temporary bundle is removed after sending
    r = await client.get(f"/articles/{ids[0]}/file")
    assert r.status_code == 200 and r.content.startswith(b"%PDF-") and r.headers["content-disposition"].startswith("inline")
    assert (await client.get(f"/articles/{ids[0]}/file?download=1")).headers["content-disposition"].startswith("attachment")
    d = await client.get(f"/articles/{ids[0]}")
    assert "Paper 0 on Graphs" in d.text and "SHA-256" in d.text and "10.1/p0" in d.text
    assert (await client.get("/articles/99999")).status_code == 404 and (await client.get("/articles/99999/file")).status_code == 404


async def test_history_users_and_user_management(client, services):
    csrf = await login(client)
    await seed_articles(services, 1)
    async with session_scope() as s:
        u = (await s.execute(select(User))).scalar_one()
        s.add(DownloadJob(user_id=u.id, query_text="10.1/p0", query_type="doi", status="sent")); uid = u.id
    h = {"X-CSRF-Token": csrf}
    assert "10.1/p0" in (await client.get("/history")).text and "10.1/p0" in (await client.get("/history?partial=1&status=sent")).text
    assert "10.1/p0" not in (await client.get("/history?status=failed")).text
    r = await client.get("/users?q=reader"); assert "@reader" in r.text and "900" in r.text
    assert "10.1/p0" in (await client.get(f"/users/{uid}")).text
    await client.post(f"/users/{uid}/block", data={"block": 1}, headers=h)
    await client.post(f"/users/{uid}/quota", data={"quota": "7"}, headers=h)
    async with session_scope() as s:
        u = await s.get(User, uid); assert u.is_blocked and u.quota_daily == 7
    await client.post(f"/users/{uid}/quota", data={"quota": "abc"}, headers=h)
    await client.post(f"/users/{uid}/block", data={"block": 0}, headers=h)
    await client.post(f"/users/{uid}/quota", data={"quota": ""}, headers=h)
    async with session_scope() as s:
        u = await s.get(User, uid); assert not u.is_blocked and u.quota_daily is None


async def test_sources_page_toggle_priority_and_connection_test(client, services):
    csrf = await login(client); h = {"X-CSRF-Token": csrf}
    r = await client.get("/sources")
    assert all(n in r.text for n in ["crossref", "openalex", "unpaywall", "doaj", "europe_pmc", "arxiv", "core"])
    await client.post("/sources/crossref/update", data={"priority": "5", "enabled": "1"}, headers=h)
    await client.post("/sources/doaj/update", data={"priority": "55"}, headers=h)      # checkbox absent => disabled
    assert services.providers.config["crossref"]["priority"] == 5 and services.providers.config["doaj"]["enabled"] is False
    assert (await client.post("/sources/doaj/update", data={"priority": "0"}, headers=h)).status_code == 303
    assert services.providers.config["doaj"]["priority"] == 55
    assert (await client.post("/sources/nope/update", data={"priority": "5"}, headers=h)).status_code == 404
    await client.post("/sources/crossref/test", headers=h)
    page = (await client.get("/sources")).text                      # flash message is shown once, in Persian by default
    assert "crossref: اتصال برقرار است" in page
    async with session_scope() as s:
        from app.database.models import ProviderConfig
        assert (await s.get(ProviderConfig, "crossref")).last_status == "ok"


async def test_settings_validation_and_secrets_are_never_displayed(client, services, tmp_path, monkeypatch):
    env = tmp_path / "secrets.env"
    import app.core.secrets_store as ss
    monkeypatch.setattr(ss, "ENV_FILE", env)
    csrf = await login(client); h = {"X-CSRF-Token": csrf}
    secret = "SuperSecretTokenValue-98765"
    r = await client.post("/sources/key", data={"env_name": "CORE_API_KEY", "value": secret}, headers=h)
    assert r.status_code == 303 and secret in env.read_text()
    for page in ("/settings", "/sources", "/reports", "/logs"):
        assert secret not in (await client.get(page)).text, page
    assert services.settings.core_api_key == secret
    assert (await client.post("/sources/key", data={"env_name": "SECRET_KEY", "value": "x"}, headers=h)).status_code == 303   # not an allowed secret name
    assert "SECRET_KEY" not in env.read_text()          # only whitelisted secret names can be written
    assert (await client.post("/sources/key", data={"env_name": "CORE_API_KEY", "value": "a\nSECRET_KEY=owned"}, headers=h)).status_code == 303
    assert "owned" not in env.read_text()
    # numeric settings are range-checked and applied live
    r = await client.post("/settings", data={"concurrency": "4", "max_pdf_mb": "30", "log_level": "WARNING", "default_language": "en"}, headers=h)
    from app.core.runtime_settings import runtime
    assert runtime.get("concurrency") == 4 and services.tasks.concurrency == 4 and runtime.get("max_pdf_mb") == 30
    await client.post("/settings", data={"max_pdf_mb": "999", "user_daily_quota": "abc"}, headers=h)
    assert runtime.get("max_pdf_mb") == 30 and runtime.get("user_daily_quota") == 20
    assert 'type="password"' in (await client.get("/settings")).text and secret not in (await client.get("/settings")).text
    # bot admin IDs: several numeric IDs are accepted, anything else is rejected and the old value is kept
    from app.bot.middleware.admin import is_bot_admin
    await client.post("/settings", data={"admin_bale_id": "123, 456"}, headers=h)
    assert is_bot_admin(123) and is_bot_admin(456) and not is_bot_admin(789)
    await client.post("/settings", data={"admin_bale_id": "abc; DROP"}, headers=h)
    assert runtime.get("admin_bale_id") == "123, 456"
    await client.post("/settings", data={"admin_bale_id": ""}, headers=h)
    assert not is_bot_admin(123)


async def test_admin_management_rules(client, services):
    csrf = await login(client); h = {"X-CSRF-Token": csrf}
    await client.post("/admins/create", data={"username": "second", "password": "Another-Pass-1", "role": "admin"}, headers=h)
    await client.post("/admins/create", data={"username": "weak", "password": "123", "role": "admin"}, headers=h)
    await client.post("/admins/create", data={"username": "second", "password": "Another-Pass-1", "role": "admin"}, headers=h)   # duplicate
    async with session_scope() as s:
        names = [a.username for a in (await s.execute(select(Admin))).scalars()]
        assert sorted(names) == ["root", "second"]
        root = (await s.execute(select(Admin).where(Admin.username == "root"))).scalar_one()
        stored = (await s.execute(select(Admin).where(Admin.username == "second"))).scalar_one()
        assert stored.password_hash.startswith("$argon2") and "Another-Pass-1" not in stored.password_hash
    await client.post(f"/admins/{root.id}/update", data={"role": "viewer", "active": "1"}, headers=h)       # last super admin
    await client.post(f"/admins/{root.id}/update", data={"role": "superadmin", "active": ""}, headers=h)
    async with session_scope() as s:
        r = await s.get(Admin, root.id); assert r.role == "superadmin" and r.is_active
    assert "admin_created" in (await client.get("/admins")).text


async def test_reports_logs_charts_and_overview(client, services):
    await login(client)
    await seed_articles(services, 2)
    async with session_scope() as s:
        from app.database.models import ErrorEvent
        s.add(ErrorEvent(kind="provider", source="crossref", message="boom <script>alert(1)</script>"))
        s.add(DownloadJob(query_text="a", query_type="doi", status="sent")); s.add(DownloadJob(query_text="b", query_type="doi", status="failed", error_code="not_found"))
    r = await client.get("/reports")
    assert "crossref" in r.text and "<script>alert(1)" not in r.text and "&lt;script&gt;" in r.text       # output is escaped
    csv_ = await client.get("/reports/errors.csv"); assert "crossref" in csv_.text
    charts = (await client.get("/api/stats/charts")).json()
    assert len(charts["days"]) == 30 and sum(charts["requests"]) == 2 and len(charts["weeks"]) == 8 and "labels" in charts["sources"]
    cards = await client.get("/dashboard/cards")
    assert "50%" in cards.text
    home = await client.get("/")
    assert 'id="charts"' in home.text and "chart.min.js" in home.text
    services.settings.log_dir.joinpath("app.log").write_text(json.dumps({"ts": "t", "level": "ERROR", "logger": "x", "msg": "visible-log-line"}) + "\n")
    assert "visible-log-line" in (await client.get("/logs")).text and "visible-log-line" not in (await client.get("/logs?level=INFO&partial=1")).text


async def test_language_theme_and_rtl_switching(client):
    csrf = await login(client)
    fa = await client.get("/")
    assert 'dir="rtl"' in fa.text and 'lang="fa"' in fa.text and "داشبورد" in fa.text
    await client.post("/prefs", data={"lang": "en", "theme": "dark"}, headers={"X-CSRF-Token": csrf, "referer": "http://testserver/articles"})
    en = await client.get("/")
    assert 'dir="ltr"' in en.text and 'lang="en"' in en.text and 'data-theme="dark"' in en.text and "Dashboard" in en.text
    async with session_scope() as s:
        a = (await s.execute(select(Admin))).scalar_one(); assert a.language == "en" and a.theme == "dark"
    r = await client.post("/prefs", data={"lang": "en"}, headers={"X-CSRF-Token": csrf, "referer": "http://evil.example/x"})
    assert r.headers["location"] == "/"                                           # no open redirect via Referer
    login_page = await httpx.AsyncClient(transport=client._transport, base_url="http://testserver").get("/login")
    assert 'dir="rtl"' in login_page.text                                         # default language is Persian


async def test_static_assets_are_served_locally(client):
    for path in ("/static/app.css", "/static/app.js", "/static/vendor/htmx.min.js", "/static/vendor/chart.min.js", "/static/vendor/Vazirmatn.woff2"):
        assert (await client.get(path)).status_code == 200, path
    html = (await client.get("/login")).text
    assert "http://" not in html.replace("http://testserver", "") and "https://" not in html      # no CDN / external resources
