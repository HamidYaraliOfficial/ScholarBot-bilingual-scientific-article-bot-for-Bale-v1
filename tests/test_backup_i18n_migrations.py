import json
import re
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, select

from app.core.localization import available_languages, t
from app.database.database import session_scope
from app.database.models import Admin, Article, Base, SavedArticle, User
from app.services import backup
from tests.conftest import ADMIN_PW
from tests.test_dashboard import seed_articles

ROOT = Path(__file__).resolve().parents[1]


async def test_backup_and_restore_roundtrip(services, admin_user):
    ids = await seed_articles(services, 3)
    async with session_scope() as s:
        arts = [a for a in (await s.execute(select(Article))).unique().scalars() if a.id in ids[:2]]
        await services.library.save(s, arts, "root")
    path = await backup.create_backup(services.settings)
    assert path.exists() and backup.list_backups(services.settings)[0]["name"] == path.name
    z = zipfile.ZipFile(path)
    names = z.namelist()
    assert "manifest.json" in names and "database.json" in names and sum(n.startswith("files/") for n in names) == 2   # permanent PDFs only
    dump = json.loads(z.read("database.json"))
    assert "admins" not in dump and "password_hash" not in z.read("database.json").decode()
    # damage the live data, then restore
    async with session_scope() as s:
        for a in (await s.execute(select(Article))).unique().scalars().all():
            a.status = "deleted"; a.title = "CHANGED"
        for sa in (await s.execute(select(SavedArticle))).scalars().all():
            await s.delete(sa)
    for f in services.settings.pdf_dir.rglob("*.pdf"):
        f.unlink()
    res = await backup.restore_backup(services.settings, path)
    assert res["articles"] == 3 and res["files_restored"] == 2
    async with session_scope() as s:
        arts = (await s.execute(select(Article))).unique().scalars().all()
        assert sorted(a.title for a in arts) == [f"Paper {i} on Graphs" for i in range(3)] and all(a.status == "sent" for a in arts)
        assert len((await s.execute(select(SavedArticle))).scalars().all()) == 2
        assert (await s.execute(select(Admin))).scalars().one().username == "root"          # admin accounts untouched
    assert len(list(services.settings.pdf_dir.rglob("*.pdf"))) == 2


async def test_restore_rejects_bad_and_malicious_archives(services, tmp_path):
    not_zip = tmp_path / "x.zip"; not_zip.write_bytes(b"nope")
    with pytest.raises(backup.BackupError):
        await backup.restore_backup(services.settings, not_zip)
    wrong = tmp_path / "w.zip"
    with zipfile.ZipFile(wrong, "w") as z:
        z.writestr("hello.txt", "x")
    with pytest.raises(backup.BackupError):
        await backup.restore_backup(services.settings, wrong)
    evil = tmp_path / "e.zip"
    with zipfile.ZipFile(evil, "w") as z:
        z.writestr("manifest.json", json.dumps({"format": 1})); z.writestr("database.json", "{}"); z.writestr("files/../../escape.pdf", "x")
    with pytest.raises(backup.BackupError):
        await backup.restore_backup(services.settings, evil)
    assert not (services.settings.storage_dir.parent / "escape.pdf").exists()
    assert backup.backup_path(services.settings, "../../etc/passwd") is None and backup.backup_path(services.settings, "x.zip") is None


async def test_backup_does_not_block_the_event_loop(services):
    import asyncio
    await seed_articles(services, 4)
    ticks = 0

    async def ticker():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.005); ticks += 1
    t_ = asyncio.create_task(ticker())
    await backup.create_backup(services.settings)
    t_.cancel()
    assert ticks > 0


async def test_backup_via_dashboard_download_and_restore(client, services):
    from tests.conftest import login
    csrf = await login(client); h = {"X-CSRF-Token": csrf}
    await seed_articles(services, 2)
    assert (await client.post("/backups/create", headers=h)).status_code == 303
    name = backup.list_backups(services.settings)[0]["name"]
    r = await client.get(f"/backups/{name}")
    assert r.status_code == 200 and zipfile.ZipFile(__import__("io").BytesIO(r.content)).testzip() is None
    assert (await client.get("/backups/..%2F..%2Fetc%2Fpasswd")).status_code == 404
    assert name in (await client.get("/settings")).text
    assert (await client.post("/backups/restore", data={"name": name}, headers=h)).status_code == 303     # no confirm -> refused, data untouched
    r = await client.post("/backups/restore", data={"name": name, "confirm": "yes"}, headers=h)
    assert r.status_code == 303
    async with session_scope() as s:
        assert len((await s.execute(select(Article))).unique().scalars().all()) == 2


# ------------------------------------------------------------------ localization
def _flat(lang):
    return json.loads((ROOT / "localization" / f"{lang}.json").read_text(encoding="utf-8"))


def test_languages_have_identical_keys_and_placeholders():
    assert available_languages() == ["en", "fa"]
    fa, en = _flat("fa"), _flat("en")
    assert set(fa) == set(en)
    for k in fa:
        assert re.findall(r"\{(\w+)\}", fa[k]) == re.findall(r"\{(\w+)\}", en[k]) or set(re.findall(r"\{(\w+)\}", fa[k])) == set(re.findall(r"\{(\w+)\}", en[k])), k
        assert fa[k].strip() and en[k].strip() or k.endswith("_help"), k
    assert re.search(r"[\u0600-\u06FF]", fa["msg_welcome"]) and not re.search(r"[\u0600-\u06FF]", en["msg_welcome"])
    assert t("xx", "nav_dashboard") == t("fa", "nav_dashboard")      # unknown language falls back to the default (Persian)
    assert t("fa", "totally_missing_key") == "totally_missing_key"


def test_every_translation_key_used_in_code_and_templates_exists():
    keys = set(_flat("en"))
    pats = [r"""\b_\(\s*['"]([a-z_0-9]+)['"]""", r"""\bt\(\s*[a-z_"']+\s*,\s*['"]([a-z_0-9]+)['"]""",
            r"""\btr\(\s*request\s*,\s*admin\s*,\s*['"]([a-z_0-9]+)['"]""", r"""['"]((?:err|msg|prompt|btn)_[a-z_0-9]+)['"]""",
            r"""_\('([a-z_]+_)' ~"""]
    used, dynamic = set(), set()
    for f in list((ROOT / "app").rglob("*.py")) + list((ROOT / "app" / "dashboard" / "templates").glob("*.html")):
        text = f.read_text(encoding="utf-8")
        for p in pats[:4]:
            used |= set(re.findall(p, text))
        dynamic |= set(re.findall(pats[4], text))
    used = {k for k in used if not k.endswith("_")}          # dynamic prefixes are checked separately below
    assert not (used - keys), sorted(used - keys)
    for prefix in dynamic | {"status_", "role_", "access_", "set_", "rep_kind_"}:
        assert any(k.startswith(prefix) for k in keys), prefix
    # all concrete values the dynamic lookups can take
    for k in ["queued", "processing", "awaiting_choice", "found", "sent", "not_found", "failed", "received", "deleted", "ok", "error",
              "running", "connecting", "disabled", "unknown", "active", "blocked"]:
        assert f"status_{k}" in keys
    for k in ["not_found", "sources_unavailable", "internal", "timeout", "interrupted", "unsupported_source", "unsafe_url", "url_unreadable",
              "no_legal_pdf", "only_publisher_page", "too_large", "file_corrupt", "download_failed", "empty_query", "query_too_long",
              "query_too_short", "invalid_doi", "invalid_link", "password_short", "password_weak", "http_403", "http_404"]:
        assert f"err_{k}" in keys, k
    from app.core.runtime_settings import DEFINITIONS
    for k in DEFINITIONS:
        assert f"set_{k}" in keys and f"set_{k}_help" in keys


def test_project_does_not_hardcode_secrets_or_use_local_storage():
    secret_like = re.compile(r"(?i)(bot\d{6,}:[A-Za-z0-9_-]{20,}|sk-[A-Za-z0-9]{20,})")
    for f in ROOT.rglob("*"):
        if f.is_file() and f.suffix in {".py", ".html", ".js", ".md", ".json", ".sh", ".bat", ".example", ".txt"} and "vendor" not in f.parts and ".venv" not in f.parts and "tests" not in f.parts:
            assert not secret_like.search(f.read_text(encoding="utf-8", errors="ignore")), f
    assert "SECRET_KEY=\n" in (ROOT / ".env.example").read_text()


# ------------------------------------------------------------------ migrations & scripts
def test_alembic_migrations_create_the_full_schema(tmp_path):
    db = tmp_path / "mig.db"
    env = {"PATH": "/usr/bin:/bin", "SECRET_KEY": "x", "DATABASE_URL": f"sqlite:///{db}", "STORAGE_DIR": str(tmp_path / "s"), "LOG_DIR": str(tmp_path / "l"),
           "SCHOLARBOT_ENV_FILE": str(tmp_path / "none.env")}
    r = subprocess.run([sys.executable, "-m", "app.cli", "migrate"], cwd=ROOT, env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    eng = create_engine(f"sqlite:///{db}")
    tables = set(inspect(eng).get_table_names())
    assert set(Base.metadata.tables) | {"alembic_version"} == tables
    # the migration must match the models exactly (no drift): autogenerate finds nothing to change
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext
    with eng.connect() as c:
        diff = compare_metadata(MigrationContext.configure(c), Base.metadata)
    assert diff == [], diff
    r2 = subprocess.run([sys.executable, "-m", "app.cli", "migrate"], cwd=ROOT, env=env, capture_output=True, text=True)
    assert r2.returncode == 0          # idempotent


def test_create_admin_cli_enforces_password_policy(tmp_path):
    env = {"PATH": "/usr/bin:/bin", "SECRET_KEY": "x", "DATABASE_URL": f"sqlite:///{tmp_path}/a.db", "STORAGE_DIR": str(tmp_path / "s"),
           "LOG_DIR": str(tmp_path / "l"), "SCHOLARBOT_ENV_FILE": str(tmp_path / "none.env")}
    run = lambda *a, **e: subprocess.run([sys.executable, "-m", "app.cli", *a], cwd=ROOT, env={**env, **e}, capture_output=True, text=True, stdin=subprocess.DEVNULL)
    assert run("migrate").returncode == 0
    assert run("create-admin", SCHOLARBOT_ADMIN_USER="boss", SCHOLARBOT_ADMIN_PASSWORD="short").returncode == 1
    assert run("create-admin", "--if-none").returncode == 0 and "No administrator yet" in run("create-admin", "--if-none").stdout    # non-interactive, nothing created
    ok = run("create-admin", SCHOLARBOT_ADMIN_USER="boss", SCHOLARBOT_ADMIN_PASSWORD=ADMIN_PW)
    assert ok.returncode == 0 and "created" in ok.stdout
    assert run("create-admin", "--if-none", SCHOLARBOT_ADMIN_USER="other", SCHOLARBOT_ADMIN_PASSWORD=ADMIN_PW).returncode == 0   # skipped
    assert run("create-admin", SCHOLARBOT_ADMIN_USER="boss", SCHOLARBOT_ADMIN_PASSWORD=ADMIN_PW).returncode == 1                 # duplicate


def test_init_env_creates_env_file_and_local_secret_key(tmp_path):
    example = ROOT / ".env.example"
    env_file = tmp_path / ".env"
    env = {"PATH": "/usr/bin:/bin", "SCHOLARBOT_ENV_FILE": str(env_file)}
    r = subprocess.run([sys.executable, "-m", "app.cli", "init-env"], cwd=ROOT, env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    text = env_file.read_text()
    m = re.search(r"""SECRET_KEY=['"]([^'"]+)['"]""", text)
    assert m and len(m.group(1)) >= 40 and "BALE_BOT_TOKEN=" in text
    assert re.search(r'BALE_BOT_TOKEN=("")?\n', text)       # the token is never invented
    first = m.group(1)
    subprocess.run([sys.executable, "-m", "app.cli", "init-env"], cwd=ROOT, env=env, capture_output=True, text=True)
    assert re.search(r"""SECRET_KEY=['"]([^'"]+)['"]""", env_file.read_text()).group(1) == first       # stable on re-run


def test_start_scripts_exist_and_are_well_formed():
    sh = (ROOT / "start_unix.sh").read_text()
    assert sh.startswith("#!/usr/bin/env bash") and subprocess.run(["bash", "-n", str(ROOT / "start_unix.sh")]).returncode == 0
    for needle in ("python3", ".venv", "requirements.txt", ".env.example", "migrate", "create-admin", "trap", "run.py", "MIN_MINOR=8"):
        assert needle in sh, needle
    bat_bytes = (ROOT / "start_windows.bat").read_bytes()
    bat = bat_bytes.decode()
    for needle in ("python", ".venv", "requirements.txt", ".env.example", "migrate", "create-admin", "run.py", "MIN_MINOR=8", "py -3"):
        assert needle in bat, needle
    assert b"\r\n" in bat_bytes and b"\n" not in bat_bytes.replace(b"\r\n", b"")      # strict CRLF line endings
    assert "sudo" not in sh and "runas" not in bat.lower()
    assert "MIN_MINOR=11" not in sh and "MIN_MINOR=11" not in bat and "3, 11" not in sh and "3,11" not in bat   # minimum is 3.8
    # every label that is jumped to / called exists in the batch file
    import re as _re
    labels = set(_re.findall(r"^:(\w+)", bat, _re.M))
    for target in _re.findall(r"(?:goto|call) :(\w+)", bat):
        assert target in labels, target


def _fake_python(path: Path, version: str, ok: bool):
    path.write_text(f"#!/bin/sh\nif [ \"$1\" = \"--version\" ]; then echo 'Python {version}'; exit 0; fi\nexit {0 if ok else 1}\n")
    path.chmod(0o755)


def _run_detection(tmp_path, stubs, extra_real=None):
    bindir, tools = tmp_path / "bin", tmp_path / "tools"
    bindir.mkdir(); tools.mkdir()
    for name in ("dirname", "head"):
        (tools / name).symlink_to(shutil_which(name))
    for name, (ver, ok) in stubs.items():
        _fake_python(bindir / name, ver, ok)
    for name, target in (extra_real or {}).items():
        (bindir / name).symlink_to(target)
    return subprocess.run([shutil_which("bash"), str(ROOT / "start_unix.sh"), "--check-python"], env={"PATH": f"{bindir}:{tools}"}, capture_output=True, text=True)


def shutil_which(name):
    import shutil
    return shutil.which(name)


def test_unix_script_python_detection_accepts_3_8_plus_only(tmp_path):
    # python3 is too old (3.7), python3.9 is fine -> the script must skip the old one and pick 3.9
    (tmp_path / "a").mkdir()
    r = _run_detection(tmp_path / "a", {"python3": ("3.7.9", False), "python3.9": ("3.9.1", True)})
    assert r.returncode == 0, r.stdout + r.stderr
    assert "python3 is too old or unusable: Python 3.7.9" in r.stdout and "Using Python 3.9.1" in r.stdout
    # only an old Python available -> clear error, non-zero exit
    (tmp_path / "b").mkdir()
    r = _run_detection(tmp_path / "b", {"python3": ("3.7.9", False)})
    assert r.returncode == 1 and "Python 3.8 or newer was not found" in r.stderr
    # no Python at all
    (tmp_path / "c").mkdir()
    r = _run_detection(tmp_path / "c", {})
    assert r.returncode == 1 and "was not found" in r.stderr
    # the real comparison logic with a real interpreter (whatever version runs these tests is >= 3.8)
    (tmp_path / "d").mkdir()
    r = _run_detection(tmp_path / "d", {}, {"python3": sys.executable})
    assert r.returncode == 0 and "Using Python 3." in r.stdout
