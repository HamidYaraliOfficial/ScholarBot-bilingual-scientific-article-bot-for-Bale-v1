"""Structural validation of downloaded PDFs."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from pypdf import PdfReader


@dataclass
class PDFCheck:
    ok: bool
    reason: str | None = None  # empty | too_large | bad_signature | truncated | encrypted | corrupt
    pages: int | None = None


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while block := f.read(chunk):
            h.update(block)
    return h.hexdigest()


def validate_pdf(path: Path, max_bytes: int | None = None) -> PDFCheck:
    """Blocking; call through app.core.compat.run_blocking."""
    try:
        size = path.stat().st_size
    except OSError:
        return PDFCheck(False, "empty")
    if size < 100:
        return PDFCheck(False, "empty")
    if max_bytes and size > max_bytes:
        return PDFCheck(False, "too_large")
    with path.open("rb") as f:
        head = f.read(1024)
        f.seek(max(0, size - 2048))
        tail = f.read()
    if b"%PDF-" not in head:
        return PDFCheck(False, "bad_signature")
    if b"%%EOF" not in tail:
        return PDFCheck(False, "truncated")
    try:
        reader = PdfReader(str(path), strict=False)
        if reader.is_encrypted and not reader.decrypt(""):
            return PDFCheck(False, "encrypted")
        pages = len(reader.pages)
    except Exception:  # noqa: BLE001 - pypdf raises many types for damaged files
        return PDFCheck(False, "corrupt")
    return PDFCheck(pages > 0, None if pages > 0 else "corrupt", pages)
