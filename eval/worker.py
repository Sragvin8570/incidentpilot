from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.agent.runner import AgentRunResult, AgentRunner  # noqa: E402
from app.agent.tools.base import ToolRegistry  # noqa: E402
from app.db.models import Incident, IncidentState  # noqa: E402
from app.incidents.state_machine import transition_incident  # noqa: E402
from app.workers import tasks  # noqa: E402
from app.workers.celery_app import celery_app  # noqa: E402
from sqlalchemy import select  # noqa: E402


def _install_no_tools() -> None:
    async def alert_only_run(
        runner: AgentRunner,
        incident_id: UUID,
        incident_context: dict,
        _tools: ToolRegistry,
    ) -> AgentRunResult:
        alert = incident_context.get("alert", {})
        messages = [
            {
                "role": "user",
                "content": json.dumps(alert, ensure_ascii=False, default=str),
            }
        ]
        loop = asyncio.get_running_loop()
        deadline = loop.time() + runner.limits.timeout_seconds
        try:
            return await asyncio.wait_for(
                runner._investigate(
                    incident_id=incident_id,
                    messages=messages,
                    tools=ToolRegistry([]),
                    deadline=deadline,
                ),
                timeout=runner.limits.timeout_seconds,
            )
        except TimeoutError:
            return AgentRunResult(
                status="escalated",
                evidence_ids=(),
                error="investigation_timeout",
                deadline=deadline,
            )

    AgentRunner.run = alert_only_run


def _install_no_verification() -> None:
    original_execute = tasks.execute_ready_incident
    original_send_task = celery_app.send_task

    async def execute_and_resolve(*args, **kwargs) -> bool:
        succeeded = await original_execute(*args, **kwargs)
        if not succeeded:
            return False
        incident_id = args[0] if args else kwargs["incident_id"]
        session_factory = args[1] if len(args) > 1 else kwargs["session_factory"]
        async with session_factory() as session:
            incident = await session.scalar(
                select(Incident)
                .where(Incident.id == incident_id)
                .with_for_update()
            )
            if incident is not None and incident.state == IncidentState.VERIFYING:
                from datetime import datetime, timezone

                incident.resolved_at = datetime.now(timezone.utc)
                await transition_incident(
                    session,
                    incident,
                    IncidentState.RESOLVED,
                    detail={
                        "stage": "evaluation_baseline",
                        "verification_skipped": True,
                    },
                )
                await session.commit()
        return True

    def send_task(name, *args, **kwargs):
        if name == "incidentpilot.verify_incident":
            return None
        return original_send_task(name, *args, **kwargs)

    tasks.execute_ready_incident = execute_and_resolve
    celery_app.send_task = send_task


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", choices=("full", "no_tools", "no_verification"), required=True)
    args = parser.parse_args()
    if args.config == "no_tools":
        _install_no_tools()
    elif args.config == "no_verification":
        _install_no_verification()
    celery_app.worker_main(
        [
            "worker",
            "--loglevel=WARNING",
            "--pool=solo",
        ]
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
