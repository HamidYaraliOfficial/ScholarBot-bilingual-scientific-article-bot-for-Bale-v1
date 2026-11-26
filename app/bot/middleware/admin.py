"""Who may use the in-bot admin panel: numeric Bale IDs from ADMIN_BALE_IDS (.env) and Settings > Primary admin Bale ID."""
from __future__ import annotations

import re

from app.core.config import get_settings
from app.core.runtime_settings import runtime


def admin_ids() -> set[int]:
    ids: set[int] = set()
    for raw in (runtime.get("admin_bale_id"), get_settings().admin_bale_ids):
        for part in re.split(r"[,;\s]+", str(raw or "")):
            if part.isdigit():
                ids.add(int(part))
    return ids


def is_bot_admin(bale_id: int | None) -> bool:
    return bale_id is not None and int(bale_id) in admin_ids()
