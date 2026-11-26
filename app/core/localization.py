"""JSON based i18n. Add a language by dropping <code>.json into /localization."""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

LOCALE_DIR = Path(__file__).resolve().parents[2] / "localization"
DEFAULT_LANG = "fa"
RTL_LANGS = {"fa", "ar", "he", "ur"}


@lru_cache
def _load(lang: str) -> dict[str, str]:
    path = LOCALE_DIR / f"{lang}.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def available_languages() -> list[str]:
    return sorted(p.stem for p in LOCALE_DIR.glob("*.json"))


def normalize_lang(lang: str | None) -> str:
    return lang if lang in available_languages() else DEFAULT_LANG


def t(lang: str, key: str, **kw) -> str:
    text = _load(normalize_lang(lang)).get(key) or _load("en").get(key) or key
    if kw:
        try:
            return text.format(**kw)
        except (KeyError, IndexError, ValueError):
            return text
    return text


def all_labels(key: str) -> set[str]:
    """Every translation of one key (used to match reply-keyboard button presses)."""
    return {_load(l).get(key, "") for l in available_languages()} - {""}


def is_rtl(lang: str) -> bool:
    return lang in RTL_LANGS


def clear_cache() -> None:
    _load.cache_clear()
