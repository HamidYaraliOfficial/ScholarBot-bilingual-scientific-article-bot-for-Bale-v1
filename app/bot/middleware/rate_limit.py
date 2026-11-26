from __future__ import annotations

from app.core.runtime_settings import runtime
from app.core.security import RateLimiter

_limiters: dict[int, RateLimiter] = {}


def check_user_rate(bale_id: int) -> tuple[bool, int]:
    """Per-user requests-per-minute limit. Returns (allowed, retry_after_seconds)."""
    limit = runtime.get("user_rate_per_minute")
    lim = _limiters.get(limit)
    if lim is None:
        _limiters.clear()
        lim = _limiters[limit] = RateLimiter(limit, 60)
    key = str(bale_id)
    return (True, 0) if lim.allow(key) else (False, lim.retry_after(key))
