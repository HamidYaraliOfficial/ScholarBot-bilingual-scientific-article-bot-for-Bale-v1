"""Bounded async worker pool that processes download jobs."""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from sqlalchemy import select

from app.database.database import session_scope
from app.database.models import DownloadJob, utcnow
from app.services.pipeline import ArticlePipeline

log = logging.getLogger(__name__)
JOB_TIMEOUT_SECONDS = 300
RESUME_WINDOW = timedelta(minutes=30)


class TaskManager:
    def __init__(self, pipeline: ArticlePipeline, concurrency: int = 3):
        self.pipeline = pipeline
        self.concurrency = concurrency
        self._queue: asyncio.Queue[int | None] = asyncio.Queue()
        self._workers: list[asyncio.Task] = []
        self.active = 0
        self._exit_tokens = 0  # pending 'please exit' sentinels (not real jobs)

    @property
    def queued(self) -> int:
        return max(0, self._queue.qsize() - self._exit_tokens)

    async def start(self) -> None:
        self._spawn(self.concurrency)
        await self._resume_unfinished()

    def _spawn(self, n: int) -> None:
        for _ in range(n):
            self._workers.append(asyncio.create_task(self._worker(), name="scholarbot-worker"))

    async def resize(self, n: int) -> None:
        self._workers = [w for w in self._workers if not w.done()]
        if n > len(self._workers):
            self._spawn(n - len(self._workers))
        else:
            for _ in range(len(self._workers) - n):
                self._exit_tokens += 1
                self._queue.put_nowait(None)  # a worker exits when it picks this up
        self.concurrency = n

    async def submit(self, job_id: int) -> None:
        await self._queue.put(job_id)

    async def stop(self) -> None:
        for w in self._workers:
            w.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()

    async def _worker(self) -> None:
        while True:
            job_id = await self._queue.get()
            if job_id is None:
                self._exit_tokens = max(0, self._exit_tokens - 1)
                return
            self.active += 1
            try:
                await asyncio.wait_for(self.pipeline.run_job(job_id), JOB_TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                log.error("job %s timed out", job_id)
                await self._mark_failed(job_id, "timeout")
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("job %s crashed", job_id)
                await self._mark_failed(job_id, "internal")
            finally:
                self.active -= 1

    async def _mark_failed(self, job_id: int, code: str) -> None:
        try:
            async with session_scope() as s:
                job = await s.get(DownloadJob, job_id)
                if job and job.status in ("queued", "processing"):
                    job.status, job.error_code, job.finished_at = "failed", code, utcnow()
                    s.expunge(job)
                    await self.pipeline.notifier.failure(job, code)
        except Exception:  # noqa: BLE001
            log.exception("could not mark job %s failed", job_id)

    async def _resume_unfinished(self) -> None:
        """After a restart: re-queue recent unfinished jobs, fail the stale ones."""
        async with session_scope() as s:
            rows = (await s.execute(select(DownloadJob).where(DownloadJob.status.in_(("queued", "processing"))))).unique().scalars().all()
            for job in rows:
                if utcnow() - job.created_at <= RESUME_WINDOW:
                    job.status = "queued"
                    await self.submit(job.id)
                else:
                    job.status, job.error_code, job.finished_at = "failed", "interrupted", utcnow()
