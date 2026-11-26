"""Small helpers that keep the code base working on Python 3.8+ (asyncio.to_thread needs 3.9)."""
from __future__ import annotations

import asyncio
import functools
from typing import Any, Callable, TypeVar

T = TypeVar("T")


async def run_blocking(func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Run a blocking function in the default thread pool without blocking the event loop."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, functools.partial(func, *args, **kwargs))
