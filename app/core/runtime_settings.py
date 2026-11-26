"""Admin-editable, non-secret settings stored in the AppSettings table (cached in memory)."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import AppSetting


@dataclass(frozen=True)
class SettingDef:
    key: str
    type: str  # int | str | bool | choice
    default: Any
    min: int | None = None
    max: int | None = None
    choices: tuple[str, ...] = ()
    pattern: str | None = None


DEFINITIONS: dict[str, SettingDef] = {d.key: d for d in [
    SettingDef("default_language", "choice", "fa", choices=("fa", "en")),
    SettingDef("admin_bale_id", "str", "", pattern=r"^\d+([,;\s]+\d+)*$"),
    SettingDef("max_pdf_mb", "int", 45, 1, 50),
    SettingDef("user_daily_quota", "int", 20, 1, 1000),
    SettingDef("user_rate_per_minute", "int", 6, 1, 60),
    SettingDef("concurrency", "int", 3, 1, 10),
    SettingDef("temp_retention_hours", "int", 24, 1, 24 * 90),
    SettingDef("cleanup_interval_minutes", "int", 60, 5, 24 * 60),
    SettingDef("backup_interval_hours", "int", 0, 0, 24 * 30),
    SettingDef("log_level", "choice", "INFO", choices=("DEBUG", "INFO", "WARNING", "ERROR")),
    SettingDef("provider_timeout_seconds", "int", 20, 3, 120),
    SettingDef("provider_retries", "int", 2, 0, 5),
    SettingDef("search_results_limit", "int", 5, 1, 8),
    SettingDef("require_explicit_license", "bool", False),
]}


def coerce(defn: SettingDef, raw: Any) -> Any:
    if defn.type == "int":
        v = int(raw)
        if defn.min is not None and v < defn.min or defn.max is not None and v > defn.max:
            raise ValueError(f"{defn.key} must be between {defn.min} and {defn.max}")
        return v
    if defn.type == "bool":
        return str(raw).lower() in ("1", "true", "on", "yes")
    if defn.type == "choice":
        if str(raw) not in defn.choices:
            raise ValueError(f"{defn.key} must be one of {defn.choices}")
        return str(raw)
    value = str(raw).strip()
    if defn.pattern and value and not re.match(defn.pattern, value):
        raise ValueError(f"{defn.key} has an invalid format")
    return value


class RuntimeSettings:
    def __init__(self) -> None:
        self._v: dict[str, Any] = {k: d.default for k, d in DEFINITIONS.items()}

    def get(self, key: str) -> Any:
        return self._v[key]

    def all(self) -> dict[str, Any]:
        return dict(self._v)

    async def load(self, session: AsyncSession) -> None:
        for row in (await session.execute(select(AppSetting))).scalars():
            if row.key in DEFINITIONS:
                try:
                    self._v[row.key] = coerce(DEFINITIONS[row.key], row.value)
                except (ValueError, TypeError):
                    pass

    async def set(self, session: AsyncSession, key: str, raw: Any) -> Any:
        defn = DEFINITIONS[key]
        value = coerce(defn, raw)
        row = await session.get(AppSetting, key)
        text = ("true" if value else "false") if defn.type == "bool" else str(value)
        if row is None:
            session.add(AppSetting(key=key, value=text))
        else:
            row.value = text
        self._v[key] = value
        return value


runtime = RuntimeSettings()
