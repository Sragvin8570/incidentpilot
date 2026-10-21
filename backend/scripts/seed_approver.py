from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from app.auth.service import seed_approver_user  # noqa: E402
from app.config import get_settings  # noqa: E402


async def main() -> None:
    settings = get_settings()
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    try:
        user = await seed_approver_user(
            async_sessionmaker(engine, expire_on_commit=False),
            settings,
        )
        if user is None:
            raise ValueError(
                "Set APPROVER_USERNAME and APPROVER_PASSWORD before seeding"
            )
        print(f"Approver account ready: {user.username}")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
