from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import LLMUsage


async def record_llm_call(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    response: Any,
) -> None:
    usage = getattr(response, "usage", None)
    session = LLMUsage(
        incident_id=incident_id,
        prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
        completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
    )
    async with session_factory() as db_session:
        db_session.add(session)
        await db_session.commit()
