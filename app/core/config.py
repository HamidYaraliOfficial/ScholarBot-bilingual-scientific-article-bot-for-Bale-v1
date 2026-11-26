"""Environment-based configuration. Secrets live only in .env / the process environment."""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parents[2]
ENV_FILE = Path(os.environ.get("SCHOLARBOT_ENV_FILE", BASE_DIR / ".env"))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=str(ENV_FILE), env_file_encoding="utf-8", extra="ignore")

    # --- secrets / credentials (never logged, never displayed) ---
    secret_key: str = Field(default="", description="Signs dashboard session cookies")
    bale_bot_token: str = ""
    openalex_api_key: str = ""
    core_api_key: str = ""

    # --- service wiring ---
    bale_api_base: str = "https://tapi.bale.ai"
    admin_bale_ids: str = ""  # comma separated numeric Bale IDs that may use the in-bot admin panel
    contact_email: str = ""  # sent to Crossref / Unpaywall / Europe PMC as the polite-pool contact
    database_url: str = "sqlite:///./storage/scholarbot.db"
    storage_dir: Path = BASE_DIR / "storage"
    log_dir: Path = BASE_DIR / "logs"
    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = 8080
    cookie_secure: bool = False  # set true when served over HTTPS
    trusted_hosts: str = ""  # optional comma separated Host allow-list
    allow_private_urls: bool = False  # tests only; keeps SSRF protection on by default
    providers_config: Path = BASE_DIR / "config" / "providers.json"  # optional first-run seed (see config/providers.example.json)

    @field_validator("storage_dir", "log_dir", "providers_config", mode="after")
    @classmethod
    def _absolute(cls, v: Path) -> Path:
        return v if v.is_absolute() else (BASE_DIR / v).resolve()

    @property
    def async_database_url(self) -> str:
        url = self.database_url
        if url.startswith("sqlite:///"):
            path = url[len("sqlite:///"):]
            if path and not path.startswith(":memory:") and not Path(path).is_absolute():
                path = str((BASE_DIR / path).resolve())
                url = "sqlite:///" + path
            return url.replace("sqlite:///", "sqlite+aiosqlite:///", 1)
        if url.startswith("postgresql://"):
            return url.replace("postgresql://", "postgresql+asyncpg://", 1)
        return url

    @property
    def sync_database_url(self) -> str:
        url = self.async_database_url
        return url.replace("sqlite+aiosqlite", "sqlite").replace("postgresql+asyncpg", "postgresql+psycopg2")

    @property
    def pdf_dir(self) -> Path:
        return self.storage_dir / "pdfs"

    @property
    def temp_dir(self) -> Path:
        return self.storage_dir / "temp"

    @property
    def backup_dir(self) -> Path:
        return self.storage_dir / "backups"

    @property
    def export_dir(self) -> Path:
        return self.storage_dir / "exports"

    def ensure_dirs(self) -> None:
        for p in (self.pdf_dir, self.temp_dir, self.backup_dir, self.export_dir, self.log_dir):
            p.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    s.ensure_dirs()
    return s


def reload_settings() -> Settings:
    get_settings.cache_clear()
    return get_settings()
