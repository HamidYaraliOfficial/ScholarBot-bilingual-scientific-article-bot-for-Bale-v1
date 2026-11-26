"""Structured (JSON-lines) logging with secret redaction."""
from __future__ import annotations

import json
import logging
import re
from logging.handlers import RotatingFileHandler

_SECRET_PATTERNS = [
    re.compile(r"bot\d{3,}:[A-Za-z0-9_\-]{10,}"),                    # Bale/Telegram style bot tokens in URLs
    re.compile(r"(?i)(api[_-]?key|token|password|secret|email)=([^&\s]+)"),
    re.compile(r"(?i)(authorization:\s*bearer\s+)[^\s]+"),
]


def redact(text: str) -> str:
    text = _SECRET_PATTERNS[0].sub("bot<redacted>", text)
    text = _SECRET_PATTERNS[1].sub(lambda m: f"{m.group(1)}=<redacted>", text)
    return _SECRET_PATTERNS[2].sub(lambda m: f"{m.group(1)}<redacted>", text)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        data = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "msg": redact(record.getMessage()),
        }
        if record.exc_info:
            data["exc"] = redact(self.formatException(record.exc_info))
        return json.dumps(data, ensure_ascii=False)


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def setup_logging(log_dir, level: str = "INFO") -> None:
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level.upper())
    fh = RotatingFileHandler(log_dir / "app.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    fh.setFormatter(JsonFormatter())
    sh = logging.StreamHandler()
    sh.setFormatter(RedactingFormatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    root.addHandler(fh)
    root.addHandler(sh)
    for noisy in ("httpx", "httpcore", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def set_level(level: str) -> None:
    logging.getLogger().setLevel(level.upper())
