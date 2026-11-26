from __future__ import annotations

import re
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import create_engine

import app.core.safe_http as safe_http
from app.core import config as config_module
from app.core.runtime_settings import DEFINITIONS, runtime
from app.database.database import dispose_engine, init_engine, session_scope
from app.database.models import Admin, Base
from app.core.security import hash_password
from app.main import create_app
from app.services.container import build_services
from tests.helpers import FakeBale, HostMock, standard_providers, pdf_host

ADMIN_PW = "Correct-Horse-9"


@pytest.fixture(autouse=True)
def _reset_runtime():
    for k, d in DEFINITIONS.items():
        runtime._v[k] = d.default
    yield


@pytest.fixture
def settings(tmp_path, monkeypatch):
    env = {"SECRET_KEY": "test-secret-key-0123456789abcdef", "DATABASE_URL": f"sqlite:///{tmp_path}/test.db",
           "STORAGE_DIR": str(tmp_path / "storage"), "LOG_DIR": str(tmp_path / "logs"), "CONTACT_EMAIL": "ops@example.org",
           "BALE_BOT_TOKEN": "", "OPENALEX_API_KEY": "", "CORE_API_KEY": ""}
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    s = config_module.reload_settings()
    yield s
    config_module.get_settings.cache_clear()


@pytest.fixture
def public_dns(monkeypatch):
    """Every hostname resolves to a public address (no real DNS in tests)."""
    async def fake(host, port):
        return ["93.184.216.34"]
    monkeypatch.setattr(safe_http, "resolve_host", fake)


@pytest_asyncio.fixture
async def db(settings):
    eng = create_engine(settings.sync_database_url)
    Base.metadata.create_all(eng)
    eng.dispose()
    init_engine(settings.async_database_url)
    yield
    await dispose_engine()


@pytest_asyncio.fixture
async def services(db, settings, public_dns):
    """Full service graph wired to mocked providers / repositories / Bale."""
    prov, dl, bale = standard_providers(), pdf_host(), FakeBale()
    settings.bale_bot_token = "123:TESTTOKEN"
    svc = build_services(settings, provider_transport=prov.transport(), download_transport=dl.transport(),
                         bale_transport=bale.transport(), concurrency=2)
    svc.mocks = {"providers": prov, "downloads": dl, "bale": bale}  # type: ignore[attr-defined]
    await svc.providers.load()
    yield svc
    await svc.runner.stop()
    await svc.tasks.stop()
    await svc.providers.aclose()


@pytest_asyncio.fixture
async def admin_user(db):
    async with session_scope() as s:
        a = Admin(username="root", password_hash=hash_password(ADMIN_PW), role="superadmin")
        s.add(a)
    return a


@pytest_asyncio.fixture
async def client(settings, services, admin_user):
    app = create_app(settings, services, start_background=False)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver", follow_redirects=False) as c:
            yield c


async def csrf_of(client: httpx.AsyncClient, path: str = "/login") -> str:
    r = await client.get(path)
    m = re.search(r'name="csrf_token" value="([^"]+)"|data-csrf="([^"]+)"', r.text)
    assert m, f"no csrf token on {path} ({r.status_code})"
    return m.group(1) or m.group(2)


async def login(client: httpx.AsyncClient, username: str = "root", password: str = ADMIN_PW) -> str:
    token = await csrf_of(client, "/login")
    r = await client.post("/login", data={"username": username, "password": password, "csrf_token": token})
    assert r.status_code == 303, r.text[:200]
    return await csrf_of(client, "/")
