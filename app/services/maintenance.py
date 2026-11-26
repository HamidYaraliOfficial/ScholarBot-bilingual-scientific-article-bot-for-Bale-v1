"""Background housekeeping: temp-file cleanup and optional scheduled backups."""
from __future__ import annotations

import asyncio
import logging
import time

from app.core.config import Settings
from app.core.runtime_settings import runtime
from app.database.database import session_scope
from app.services.backup import create_backup
from app.services.library import Library

log = logging.getLogger(__name__)


class Maintenance:
    def __init__(self, library: Library, settings: Settings):
        self.library, self.s = library, settings
        self._task: asyncio.Task | None = None
        self.last_cleanup = self.last_backup = time.monotonic()
        self.last_cleanup_result: dict | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="scholarbot-maintenance")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    async def run_cleanup(self) -> dict:
        async with session_scope() as s:
            res = await self.library.cleanup(s, runtime.get("temp_retention_hours"))
        self.last_cleanup, self.last_cleanup_result = time.monotonic(), res
        return res

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(60)
            try:
                now = time.monotonic()
                if now - self.last_cleanup >= runtime.get("cleanup_interval_minutes") * 60:
                    await self.run_cleanup()
                hours = runtime.get("backup_interval_hours")
                if hours and now - self.last_backup >= hours * 3600:
                    self.last_backup = now
                    await create_backup(self.s)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("maintenance cycle failed")
