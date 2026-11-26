"""Filesystem layout: storage/temp (temporary cache) and storage/pdfs (permanent library)."""
from __future__ import annotations

import os
import shutil
import time
import uuid
from pathlib import Path

from app.core.config import Settings, get_settings
from app.core.security import safe_filename, safe_join


class StorageManager:
    def __init__(self, settings: Settings | None = None):
        self.s = settings or get_settings()
        self.s.ensure_dirs()
        self.cache_dir = self.s.temp_dir / "files"
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    # --- locations -------------------------------------------------------------------------
    def part_path(self) -> Path:
        """Scratch path for an in-progress download."""
        return self.s.temp_dir / f"{uuid.uuid4().hex}.part"

    def root_for(self, permanent: bool) -> Path:
        return self.s.pdf_dir if permanent else self.cache_dir

    def abs_path(self, rel: str, permanent: bool) -> Path:
        return safe_join(self.root_for(permanent), rel)

    @staticmethod
    def rel_name(sha256: str, title: str) -> str:
        return f"{sha256[:2]}/{sha256[:12]}_{safe_filename(title, 60)}.pdf"

    # --- operations --------------------------------------------------------------------------
    def adopt(self, part: Path, sha256: str, title: str, permanent: bool = False) -> str:
        """Move a validated scratch file to its final place; returns the relative path."""
        rel = self.rel_name(sha256, title)
        dest = self.abs_path(rel, permanent)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            part.unlink(missing_ok=True)
        else:
            shutil.move(str(part), str(dest))
        return rel

    def move(self, rel: str, from_permanent: bool, to_permanent: bool) -> bool:
        src, dst = self.abs_path(rel, from_permanent), self.abs_path(rel, to_permanent)
        if not src.exists():
            return False
        if src == dst:
            return True
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        os.utime(dst, None)
        return True

    def delete(self, rel: str, permanent: bool) -> bool:
        try:
            p = self.abs_path(rel, permanent)
        except ValueError:
            return False
        if p.exists():
            p.unlink()
            try:
                p.parent.rmdir()  # drop empty shard directory
            except OSError:
                pass
            return True
        return False

    def exists(self, rel: str, permanent: bool) -> bool:
        try:
            return self.abs_path(rel, permanent).is_file()
        except ValueError:
            return False

    def purge_scratch(self, older_than_seconds: int = 3600) -> int:
        """Remove abandoned .part files and old export bundles."""
        removed, now = 0, time.time()
        for folder, pattern in ((self.s.temp_dir, "*.part"), (self.s.export_dir, "*")):
            for p in folder.glob(pattern):
                if p.is_file() and p.name != ".gitkeep" and now - p.stat().st_mtime > older_than_seconds:
                    p.unlink(missing_ok=True)
                    removed += 1
        return removed

    def usage(self) -> dict[str, int]:
        def size_of(folder: Path) -> tuple[int, int]:
            total = count = 0
            for p in folder.rglob("*.pdf"):
                total += p.stat().st_size
                count += 1
            return total, count

        lib_bytes, lib_n = size_of(self.s.pdf_dir)
        tmp_bytes, tmp_n = size_of(self.cache_dir)
        disk = shutil.disk_usage(self.s.storage_dir)
        return {"library_bytes": lib_bytes, "library_files": lib_n, "temp_bytes": tmp_bytes, "temp_files": tmp_n,
                "stored_bytes": lib_bytes + tmp_bytes, "disk_total": disk.total, "disk_used": disk.used,
                "disk_free": disk.free}
