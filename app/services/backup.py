"""Portable backup / restore (works for SQLite and PostgreSQL): one ZIP with database.json + permanent PDFs.

Admin accounts are intentionally excluded (no password hashes in backups; restoring never locks you out).
"""
from __future__ import annotations

import asyncio
import json
import logging
import zipfile
from datetime import date, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import DateTime, delete, insert, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.compat import run_blocking
from app.core.config import Settings
from app.core.security import safe_join
from app.database.database import get_engine, session_scope
from app.database.models import Base, utcnow

log = logging.getLogger(__name__)
BACKUP_FORMAT = 1
TABLES = ["users", "article_files", "articles", "saved_articles", "bookmarks", "download_jobs", "deliveries",
          "audit_logs", "app_settings", "provider_configs", "error_events"]
_lock = asyncio.Lock()


def _jsonable(v: Any) -> Any:
    return v.isoformat() if isinstance(v, (datetime, date)) else v


async def _dump(session: AsyncSession) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for name in TABLES:
        table = Base.metadata.tables[name]
        rows = (await session.execute(select(table))).mappings().all()
        out[name] = [{k: _jsonable(v) for k, v in r.items()} for r in rows]
    return out


async def create_backup(settings: Settings) -> Path:
    """Runs without blocking the event loop (DB reads are async, zipping happens in a worker thread)."""
    async with _lock:
        async with session_scope() as s:
            data = await _dump(s)
        stamp = utcnow().strftime("%Y%m%d_%H%M%S")
        final = settings.backup_dir / f"scholarbot_backup_{stamp}.zip"
        tmp = final.with_suffix(".zip.tmp")
        files = [(f["path"], f["sha256"]) for f in data["article_files"] if f["is_permanent"]]
        pdf_dir = settings.pdf_dir

        def write() -> int:
            n = 0
            with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
                for rel, _ in files:
                    try:
                        src = safe_join(pdf_dir, rel)
                    except ValueError:
                        continue
                    if src.is_file():
                        z.write(src, f"files/{rel}")
                        n += 1
                manifest = {"format": BACKUP_FORMAT, "created_at": utcnow().isoformat(), "app": "ScholarBot",
                            "counts": {k: len(v) for k, v in data.items()}, "files": n}
                z.writestr("manifest.json", json.dumps(manifest, indent=2))
                z.writestr("database.json", json.dumps(data, ensure_ascii=False))
            tmp.replace(final)
            return n

        n = await run_blocking(write)
        log.info("backup created: %s (%d files)", final.name, n)
        return final


def list_backups(settings: Settings) -> list[dict]:
    items = []
    for p in sorted(settings.backup_dir.glob("scholarbot_backup_*.zip"), reverse=True):
        st = p.stat()
        items.append({"name": p.name, "size": st.st_size, "created": datetime.utcfromtimestamp(st.st_mtime)})
    return items


def backup_path(settings: Settings, name: str) -> Path | None:
    if not name.startswith("scholarbot_backup_") or not name.endswith(".zip"):
        return None
    try:
        p = safe_join(settings.backup_dir, name)
    except ValueError:
        return None
    return p if p.is_file() else None


def _coerce(table_name: str, row: dict) -> dict:
    table = Base.metadata.tables[table_name]
    out = {}
    for col in table.columns:
        if col.name not in row:
            continue
        v = row[col.name]
        if v is not None and isinstance(col.type, DateTime) and isinstance(v, str):
            v = datetime.fromisoformat(v)
        out[col.name] = v
    return out


class BackupError(Exception):
    pass


async def restore_backup(settings: Settings, path: Path) -> dict[str, int]:
    def read() -> tuple[dict, dict[str, bytes]]:
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
            if "manifest.json" not in names or "database.json" not in names:
                raise BackupError("not a ScholarBot backup")
            manifest = json.loads(z.read("manifest.json"))
            if manifest.get("format") != BACKUP_FORMAT:
                raise BackupError("unsupported backup format")
            for n in names:
                if n.startswith("/") or ".." in Path(n).parts:
                    raise BackupError("unsafe path in backup")
            return json.loads(z.read("database.json")), {}

    try:
        data, _ = await run_blocking(read)
    except (zipfile.BadZipFile, json.JSONDecodeError, KeyError) as exc:
        raise BackupError("corrupt backup file") from exc
    async with _lock:
        async with session_scope() as s:
            for name in reversed(TABLES):
                await s.execute(delete(Base.metadata.tables[name]))
            for name in TABLES:
                rows = [_coerce(name, r) for r in data.get(name, [])]
                if rows:
                    await s.execute(insert(Base.metadata.tables[name]), rows)
            if get_engine().dialect.name == "postgresql":
                for name in TABLES:
                    t = Base.metadata.tables[name]
                    if "id" in t.columns:
                        await s.execute(text(f"SELECT setval(pg_get_serial_sequence('{name}','id'), "
                                             f"COALESCE((SELECT MAX(id) FROM {name}),1))"))
        pdf_dir = settings.pdf_dir

        def extract() -> int:
            n = 0
            with zipfile.ZipFile(path) as z:
                for member in z.namelist():
                    if not member.startswith("files/") or member.endswith("/"):
                        continue
                    dest = safe_join(pdf_dir, member[len("files/"):])
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    with z.open(member) as src, dest.open("wb") as dst:
                        dst.write(src.read())
                    n += 1
            return n

        files = await run_blocking(extract)
    return {**{k: len(v) for k, v in data.items()}, "files_restored": files}
