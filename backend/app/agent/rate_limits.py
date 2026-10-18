from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Awaitable, Callable, TypeVar

from openai import RateLimitError

MAX_RATE_LIMIT_RETRIES = 3
T = TypeVar("T")


@dataclass
class RateLimitRetryBudget:
    retries: int = 0


class RateLimitEscalation(RuntimeError):
    pass


def _quota_period(error: RateLimitError) -> str | None:
    try:
        details = json.dumps(error.body, default=str).lower()
    except (TypeError, ValueError):
        details = str(error.body).lower()
    if any(
        marker in details
        for marker in ("perday", "per_day", "per-day", "per day", "daily quota")
    ):
        return "day"
    if any(
        marker in details
        for marker in ("perminute", "per_minute", "per-minute", "per minute")
    ):
        return "minute"
    return None


def _retry_after_seconds(error: RateLimitError) -> float | None:
    retry_after = error.response.headers.get("Retry-After")
    if retry_after:
        try:
            return max(0.0, float(retry_after))
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(retry_after)
            except (TypeError, ValueError, OverflowError):
                retry_at = None
            if retry_at is not None:
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=timezone.utc)
                return max(
                    0.0,
                    (retry_at - datetime.now(timezone.utc)).total_seconds(),
                )
    try:
        body = json.dumps(error.body, default=str)
    except (TypeError, ValueError):
        return None
    match = re.search(r'"retryDelay"\s*:\s*"([0-9]+(?:\.[0-9]+)?)s"', body)
    return float(match.group(1)) if match else None


async def create_with_rate_limit_retry(
    create: Callable[[], Awaitable[T]],
    *,
    deadline: float,
    loop: asyncio.AbstractEventLoop,
    budget: RateLimitRetryBudget,
) -> T:
    while True:
        try:
            return await create()
        except RateLimitError as error:
            period = _quota_period(error)
            if period == "day":
                raise RateLimitEscalation("daily_quota_exceeded") from error
            if period != "minute":
                raise RateLimitEscalation(
                    "rate_limit_quota_period_unknown"
                ) from error
            if budget.retries >= MAX_RATE_LIMIT_RETRIES:
                raise RateLimitEscalation(
                    "per_minute_quota_retries_exhausted"
                ) from error
            delay = max(
                float(2**budget.retries),
                _retry_after_seconds(error) or 0.0,
            )
            if loop.time() + delay >= deadline:
                raise RateLimitEscalation(
                    "per_minute_quota_retry_window_exhausted"
                ) from error
            budget.retries += 1
            await asyncio.sleep(delay)
