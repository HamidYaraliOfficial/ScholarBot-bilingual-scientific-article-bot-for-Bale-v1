from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.localization import normalize_lang
from app.core.runtime_settings import runtime
from app.database.models import User
from app.database.repositories.users import get_or_create_user


async def load_user(session: AsyncSession, tg_user: dict) -> tuple[User, str]:
    """Create/update the user row and resolve the language to answer in."""
    user, _ = await get_or_create_user(session, int(tg_user["id"]), tg_user.get("username"), tg_user.get("first_name"))
    return user, normalize_lang(user.language or runtime.get("default_language"))
