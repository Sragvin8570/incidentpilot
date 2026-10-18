from __future__ import annotations

import asyncio
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.agent.actions import ActionCatalog  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.runbooks.embeddings import LocalRunbookEmbedder  # noqa: E402
from app.runbooks.ingestion import ingest_runbooks  # noqa: E402


async def main() -> None:
    settings = get_settings()
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    try:
        count = await ingest_runbooks(
            async_sessionmaker(engine, expire_on_commit=False),
            LocalRunbookEmbedder(settings.runbook_embedding_model),
            runbook_dir=ROOT / "runbooks",
            catalog=ActionCatalog.load(),
        )
        print(f"Ingested {count} runbook chunks using CPU embeddings.")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
