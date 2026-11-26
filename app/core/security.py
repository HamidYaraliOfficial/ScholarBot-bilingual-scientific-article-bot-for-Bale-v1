"""Password hashing, CSRF helpers, rate limiting and filesystem-safety helpers."""
from __future__ import annotations

import hmac
import re
import secrets
import time
import unicodedata
from collections import defaultdict, deque
from pathlib import Path

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

_hasher = PasswordHasher()
MIN_PASSWORD_LENGTH = 10


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(stored_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(stored_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def password_problem(password: str) -> str | None:
    """Return a localization key describing why the password is rejected, or None."""
    if len(password) < MIN_PASSWORD_LENGTH:
        return "err_password_short"
    if password.isdigit() or password.lower() == password.upper():
        return "err_password_weak"
    return None


def new_token(n: int = 32) -> str:
    return secrets.token_urlsafe(n)


def tokens_match(a: str | None, b: str | None) -> bool:
    return bool(a) and bool(b) and hmac.compare_digest(a.encode(), b.encode())


class RateLimiter:
    """Sliding-window limiter kept in memory (per process)."""

    def __init__(self, max_events: int, window_seconds: float):
        self.max_events, self.window = max_events, window_seconds
        self._events: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        q = self._events[key]
        while q and now - q[0] > self.window:
            q.popleft()
        if len(q) >= self.max_events:
            return False
        q.append(now)
        return True

    def retry_after(self, key: str) -> int:
        q = self._events.get(key)
        return max(1, int(self.window - (time.monotonic() - q[0]))) if q else 0

    def reset(self, key: str) -> None:
        self._events.pop(key, None)


def safe_filename(text: str, max_len: int = 80, fallback: str = "article") -> str:
    """ASCII-only, path-free file stem."""
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    text = re.sub(r"\.{2,}", ".", re.sub(r"[^A-Za-z0-9._-]+", "_", text)).strip("._-")
    return (text[:max_len].strip("._-")) or fallback


def safe_join(base: Path, *parts: str) -> Path:
    """Join and make sure the result stays inside `base` (blocks ../ and absolute escapes)."""
    base_r = base.resolve()
    target = base_r.joinpath(*parts).resolve()
    if base_r != target and base_r not in target.parents:
        raise ValueError("path escapes storage directory")
    return target


def secret_state(value: str) -> bool:
    return bool(value and value.strip())
