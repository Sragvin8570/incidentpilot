from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from pydantic import BaseModel, Field

from app.agent.tools.base import Tool, ToolExecutionError, ToolRegistry


class PrometheusQueryArgs(BaseModel):
    promql: str = Field(min_length=1, max_length=2000)
    range_minutes: int = Field(default=15, ge=1, le=60)


class PrometheusEvidenceTools:
    def __init__(self, base_url: str, *, timeout_seconds: float = 10) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    def registry(self) -> ToolRegistry:
        return ToolRegistry(
            [
                Tool(
                    "query_prometheus",
                    "Query Prometheus over a bounded time range and summarize the returned series.",
                    PrometheusQueryArgs,
                    self.query_prometheus,
                )
            ]
        )

    async def query_prometheus(self, args: PrometheusQueryArgs) -> dict[str, Any]:
        end = datetime.now(timezone.utc)
        start = end - timedelta(minutes=args.range_minutes)
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                response = await client.get(
                    f"{self.base_url}/api/v1/query_range",
                    params={
                        "query": args.promql,
                        "start": start.isoformat(),
                        "end": end.isoformat(),
                        "step": "60s",
                    },
                )
            response.raise_for_status()
        except httpx.HTTPError as error:
            raise ToolExecutionError(
                f"Prometheus query failed: {error}"
            ) from error

        try:
            payload = response.json()
        except ValueError as error:
            raise ToolExecutionError(
                "Prometheus returned invalid JSON"
            ) from error

        if not isinstance(payload, dict):
            raise ToolExecutionError("Prometheus returned an invalid response shape")
        if payload.get("status") != "success":
            raise ToolExecutionError("Prometheus returned a non-success response")

        summaries = []
        for series in payload.get("data", {}).get("result", []):
            values = []
            for _, value in series.get("values", []):
                try:
                    values.append(float(value))
                except (TypeError, ValueError):
                    continue
            if not values:
                continue
            summaries.append(
                {
                    "labels": series.get("metric", {}),
                    "min": min(values),
                    "max": max(values),
                    "last": values[-1],
                    "trend": "increasing"
                    if values[-1] > values[0]
                    else "decreasing"
                    if values[-1] < values[0]
                    else "steady",
                    "samples": len(values),
                }
            )
        return {"query": args.promql, "series": summaries}
