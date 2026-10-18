from __future__ import annotations

import asyncio
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.actions import ActionCatalog
from app.db.models import RunbookChunk
from app.runbooks.embeddings import LocalRunbookEmbedder

_FRONT_MATTER = re.compile(r"\A---\s*\r?\n(.*?)\r?\n---\s*(?:\r?\n|\Z)", re.S)
_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$")


@dataclass(frozen=True)
class RunbookSection:
    id: str
    runbook_id: str
    action_id: str
    title: str
    heading: str
    content: str
    metadata: dict[str, Any]


def parse_runbook(path: Path, catalog: ActionCatalog) -> list[RunbookSection]:
    text = path.read_text(encoding="utf-8")
    match = _FRONT_MATTER.match(text)
    if match is None:
        raise ValueError(f"Runbook {path.name} has no YAML front matter")
    metadata = yaml.safe_load(match.group(1))
    if not isinstance(metadata, dict):
        raise ValueError(f"Runbook {path.name} front matter must be a mapping")
    runbook_id = metadata.get("id")
    action_id = metadata.get("linked_action_id")
    title = metadata.get("title")
    if not all(isinstance(value, str) and value for value in (runbook_id, action_id, title)):
        raise ValueError(f"Runbook {path.name} is missing id, title, or linked_action_id")
    if action_id not in catalog.actions:
        raise ValueError(f"Runbook {path.name} references unknown action {action_id}")
    action = catalog.actions[action_id]
    if metadata.get("risk_level") != action.risk_level:
        raise ValueError(f"Runbook {path.name} risk_level does not match action config")

    body = text[match.end() :]
    sections: list[RunbookSection] = []
    heading = title
    lines: list[str] = []

    def append_section() -> None:
        content = "\n".join(lines).strip()
        if not content:
            return
        stable = hashlib.sha256(
            f"{runbook_id}\0{heading}\0{content}".encode("utf-8")
        ).hexdigest()
        sections.append(
            RunbookSection(
                id=stable,
                runbook_id=runbook_id,
                action_id=action_id,
                title=title,
                heading=heading,
                content=content,
                metadata=metadata,
            )
        )

    for line in body.splitlines():
        heading_match = _HEADING.match(line)
        if heading_match:
            append_section()
            heading = heading_match.group(2)
            lines = [line]
        else:
            lines.append(line)
    append_section()
    if not sections:
        raise ValueError(f"Runbook {path.name} contains no heading sections")
    return sections


def load_runbook_sections(directory: Path, catalog: ActionCatalog) -> list[RunbookSection]:
    paths = sorted(directory.glob("*.md"))
    if not paths:
        raise ValueError(f"No runbook Markdown files found in {directory}")
    sections = [
        section
        for path in paths
        for section in parse_runbook(path, catalog)
    ]
    action_ids = {section.action_id for section in sections}
    if action_ids != set(catalog.actions):
        missing = sorted(set(catalog.actions) - action_ids)
        raise ValueError(f"Action configs without a linked runbook: {missing}")
    return sections


async def ingest_runbooks(
    session_factory: async_sessionmaker[AsyncSession],
    embedder: LocalRunbookEmbedder,
    *,
    runbook_dir: Path,
    catalog: ActionCatalog,
) -> int:
    sections = load_runbook_sections(runbook_dir, catalog)
    embeddings = await asyncio.to_thread(
        embedder.encode,
        [f"{section.title}\n{section.heading}\n{section.content}" for section in sections],
    )
    async with session_factory() as session:
        await session.execute(delete(RunbookChunk))
        session.add_all(
            [
                RunbookChunk(
                    id=section.id,
                    runbook_id=section.runbook_id,
                    action_id=section.action_id,
                    title=section.title,
                    heading=section.heading,
                    content=section.content,
                    metadata_json=section.metadata,
                    embedding=embedding,
                )
                for section, embedding in zip(sections, embeddings, strict=True)
            ]
        )
        await session.commit()
    return len(sections)
