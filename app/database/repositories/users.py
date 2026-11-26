from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.runtime_settings import runtime
from app.database.models import DownloadJob, User, utcnow


async def get_or_create_user(session: AsyncSession, bale_id: int, username: str | None,
                             first_name: str | None) -> tuple[User, bool]:
    user = (await session.execute(select(User).where(User.bale_id == bale_id))).scalar_one_or_none()
    if user is None:
        user = User(bale_id=bale_id, username=username, first_name=first_name)
        session.add(user)
        await session.flush()
        return user, True
    user.last_seen_at = utcnow()
    if username:
        user.username = username
    if first_name:
        user.first_name = first_name
    return user, False


async def jobs_in_last_day(session: AsyncSession, user_id: int) -> int:
    since: datetime = utcnow() - timedelta(days=1)
    stmt = select(func.count()).select_from(DownloadJob).where(
        DownloadJob.user_id == user_id, DownloadJob.created_at >= since, DownloadJob.query_type != "pick")
    return (await session.execute(stmt)).scalar_one()


def daily_quota(user: User) -> int:
    return user.quota_daily if user.quota_daily is not None else runtime.get("user_daily_quota")
