from __future__ import annotations

import asyncio
import math
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.tools.base import Tool, ToolExecutionError, ToolRegistry
from app.db.models import Incident, IncidentState, RunbookChunk
from app.runbooks.embeddings import LocalRunbookEmbedder


class SearchRunbooksArgs(BaseModel):
    query: str = Field(min_length=3, max_length=2000)
    limit: int = Field(default=4, ge=1, le=8)


class SimilarIncidentsArgs(BaseModel):
    query: str = Field(min_length=3, max_length=2000)
    limit: int = Field(default=3, ge=1, le=5)
    exclude_incident_id: UUID | None = None


class RunbookSearchTools:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        embedder: LocalRunbookEmbedder,
    ) -> None:
        self.session_factory = session_factory
        self.embedder = embedder

    def registry(self) -> ToolRegistry:
        return ToolRegistry(
            [
                Tool(
                    "search_runbooks",
                    "Find the most relevant runbook sections for an incident. Returns linked action ids and runbook conditions.",
                    SearchRunbooksArgs,
                    self.search_runbooks,
                ),
                Tool(
                    "find_similar_incidents",
                    "Find previously analyzed incidents with similar root causes.",
                    SimilarIncidentsArgs,
                    self.find_similar_incidents,
                ),
            ]
        )

    async def _embed(self, texts: list[str]) -> list[list[float]]:
        try:
            return await asyncio.to_thread(self.embedder.encode, texts)
        except (RuntimeError, OSError, ValueError) as error:
            raise ToolExecutionError(f"Local runbook embedding failed: {error}") from error

    async def search_runbooks(self, args: SearchRunbooksArgs) -> dict[str, Any]:
        query_vector = (await self._embed([args.query]))[0]
        async with self.session_factory() as session:
            distance = RunbookChunk.embedding.cosine_distance(query_vector)
            rows = (
                await session.execute(
                    select(RunbookChunk, (1 - distance).label("similarity"))
                    .order_by(distance)
                    .limit(args.limit)
                )
            ).all()
        return {
            "results": [
                {
                    "runbook_id": row.runbook_id,
                    "linked_action_id": row.action_id,
                    "title": row.title,
                    "heading": row.heading,
                    "content": row.content,
                    "similarity": round(
                        min(1.0, max(0.0, float(similarity))), 4
                    ),
                }
                for row, similarity in rows
            ]
        }

    async def find_similar_incidents(
        self, args: SimilarIncidentsArgs
    ) -> dict[str, Any]:
        async with self.session_factory() as session:
            query = select(Incident).where(
                Incident.state == IncidentState.ANALYZED,
                Incident.rca_json.is_not(None),
            )
            if args.exclude_incident_id is not None:
                query = query.where(Incident.id != args.exclude_incident_id)
            incidents = (
                await session.scalars(
                    query.order_by(Incident.created_at.desc()).limit(100)
                )
            ).all()
        candidates = [
            (
                incident,
                incident.rca_json.get("root_cause", ""),
            )
            for incident in incidents
            if isinstance(incident.rca_json, dict)
            and incident.rca_json.get("root_cause")
        ]
        if not candidates:
            return {"results": []}
        vectors = await self._embed(
            [args.query, *[root_cause for _, root_cause in candidates]]
        )
        ranked = sorted(
            (
                (
                    _cosine_similarity(vectors[0], vector),
                    incident,
                    root_cause,
                )
                for (incident, root_cause), vector in zip(
                    candidates, vectors[1:], strict=True
                )
            ),
            key=lambda item: item[0],
            reverse=True,
        )[: args.limit]
        return {
            "results": [
                {
                    "incident_id": str(incident.id),
                    "alert_name": incident.alert_name,
                    "environment": incident.environment,
                    "target": {
                        "kind": incident.target_kind,
                        "name": incident.target_name,
                    },
                    "root_cause": root_cause,
                    "similarity": round(score, 4),
                }
                for score, incident, root_cause in ranked
            ]
        }


def _cosine_similarity(left: list[float], right: list[float]) -> float:
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if not left_norm or not right_norm:
        return 0.0
    return sum(a * b for a, b in zip(left, right, strict=True)) / (
        left_norm * right_norm
    )
