"""Write-only management of secrets in the .env file (values are never displayed or logged)."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import set_key

from app.core.config import ENV_FILE

# env var -> (Settings attribute, human label key)
SECRET_FIELDS: dict[str, tuple[str, str]] = {
    "BALE_BOT_TOKEN": ("bale_bot_token", "lbl_bale_token"),
    "OPENALEX_API_KEY": ("openalex_api_key", "lbl_openalex_key"),
    "CORE_API_KEY": ("core_api_key", "lbl_core_key"),
    "CONTACT_EMAIL": ("contact_email", "lbl_contact_email"),
}


def write_env_value(name: str, value: str, env_file: Path | None = None) -> None:
    path = env_file or ENV_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.touch(mode=0o600)
    set_key(str(path), name, value, quote_mode="always")
    os.environ[name] = value
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass  # e.g. Windows


def propagate_settings(svc) -> None:
    """Point long-lived components at the freshly loaded Settings object."""
    s = svc.settings
    svc.runner.s = s
    svc.providers.settings = s
    for p in svc.providers.providers.values():
        p.settings = s
        p.http.settings = s
    svc.search.s = s
    svc.downloader.s = s
