from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.security import hash_password
from app.config import Settings
from app.db.models import User


async def seed_approver_user(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> User | None:
    username = settings.approver_username
    password = settings.approver_password
    if username is None and password is None:
        return None
    if username is None or password is None:
        raise ValueError(
            "APPROVER_USERNAME and APPROVER_PASSWORD must be configured together"
        )
    username = username.strip()
    if not username:
        raise ValueError("APPROVER_USERNAME must not be empty")
    password_value = password.get_secret_value()
    password_hash = hash_password(password_value)
    async with session_factory() as session:
        user = await session.scalar(select(User).where(User.username == username))
        if user is not None:
            if user.role != "approver":
                raise ValueError("Configured approver username belongs to a non-approver")
            return user
        user = User(
            username=username,
            password_hash=password_hash,
            role="approver",
        )
        session.add(user)
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            user = await session.scalar(select(User).where(User.username == username))
            if user is None or user.role != "approver":
                raise
        return user
