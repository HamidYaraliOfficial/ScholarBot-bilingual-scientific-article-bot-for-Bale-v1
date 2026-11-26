"""Command line helpers used by the start scripts.

python -m app.cli init-env | migrate | create-admin [--if-none] | check
"""
from __future__ import annotations

import argparse
import getpass
import os
import secrets
import shutil
import sys

from app.core.config import BASE_DIR, ENV_FILE, get_settings, reload_settings
from app.core.security import hash_password, password_problem


def init_env() -> None:
    """Create .env from .env.example if needed and make sure the session-signing SECRET_KEY exists."""
    example = BASE_DIR / ".env.example"
    if not ENV_FILE.exists():
        if example.exists():
            shutil.copy(example, ENV_FILE)
        else:
            ENV_FILE.touch()
        print(f"Created {ENV_FILE.name} from .env.example")
    from dotenv import dotenv_values, set_key
    if not (dotenv_values(ENV_FILE).get("SECRET_KEY") or "").strip():
        set_key(str(ENV_FILE), "SECRET_KEY", secrets.token_urlsafe(48), quote_mode="always")
        print("Generated a random local SECRET_KEY (used only to sign dashboard session cookies).")
    try:
        os.chmod(ENV_FILE, 0o600)
    except OSError:
        pass
    reload_settings()


def migrate() -> None:
    from alembic import command
    from alembic.config import Config

    settings = get_settings()
    cfg = Config(str(BASE_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BASE_DIR / "migrations"))
    cfg.set_main_option("sqlalchemy.url", settings.sync_database_url.replace("%", "%%"))
    command.upgrade(cfg, "head")


def create_admin(if_none: bool = False, username: str | None = None) -> int:
    import asyncio

    from sqlalchemy import func, select

    from app.database.database import dispose_engine, init_engine, session_scope
    from app.database.models import Admin

    async def run() -> int:
        init_engine(get_settings().async_database_url)
        try:
            async with session_scope() as s:
                existing = (await s.execute(select(func.count(Admin.id)))).scalar_one()
                if if_none and existing:
                    return 0
                user = username or os.environ.get("SCHOLARBOT_ADMIN_USER") or ""
                pw = os.environ.get("SCHOLARBOT_ADMIN_PASSWORD") or ""
                if not user or not pw:
                    if not sys.stdin.isatty():
                        print("No administrator yet. Run:  python -m app.cli create-admin")
                        return 0
                    print("\nCreate the first dashboard administrator")
                    user = user or input("  Username: ").strip()
                    while True:
                        pw = getpass.getpass("  Password (min 10 chars, not only digits): ")
                        problem = password_problem(pw)
                        if problem:
                            print("  Password rejected:", problem.replace("err_password_", ""))
                            continue
                        if pw != getpass.getpass("  Repeat password: "):
                            print("  Passwords do not match.")
                            continue
                        break
                if not (3 <= len(user) <= 64) or password_problem(pw):
                    print("Invalid username or password (username 3-64 chars, password min 10 chars).")
                    return 1
                if (await s.execute(select(Admin).where(Admin.username == user))).scalar_one_or_none():
                    print("Username already exists.")
                    return 1
                s.add(Admin(username=user, password_hash=hash_password(pw), role="superadmin"))
                print(f"Administrator '{user}' created (superadmin).")
                return 0
        finally:
            await dispose_engine()

    return asyncio.run(run())


def check() -> int:
    s = get_settings()
    print("Database :", s.database_url.split("://")[0])
    print("Bale bot :", "token configured" if s.bale_bot_token.strip() else "NOT configured (dashboard only)")
    print("Contact  :", "set" if s.contact_email else "missing (Unpaywall disabled until CONTACT_EMAIL is set)")
    print("OpenAlex :", "key set" if s.openalex_api_key else "no key (OpenAlex may reject keyless requests)")
    print("CORE     :", "key set" if s.core_api_key else "no key (CORE repositories disabled)")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m app.cli")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init-env")
    sub.add_parser("migrate")
    sub.add_parser("check")
    ca = sub.add_parser("create-admin")
    ca.add_argument("--if-none", action="store_true", help="only when no administrator exists")
    ca.add_argument("--username")
    args = p.parse_args(argv)
    if args.cmd == "init-env":
        init_env()
    elif args.cmd == "migrate":
        migrate()
    elif args.cmd == "check":
        return check()
    elif args.cmd == "create-admin":
        return create_admin(args.if_none, args.username)
    return 0


if __name__ == "__main__":
    sys.exit(main())
