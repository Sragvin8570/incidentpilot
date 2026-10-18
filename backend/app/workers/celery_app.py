from celery import Celery

from app.config import get_settings

settings = get_settings()
celery_app = Celery(
    "incidentpilot",
    broker=settings.redis_url,
    backend=settings.redis_url,
)
celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    task_track_started=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    imports=("app.workers.tasks",),
    beat_schedule={
        "escalate-stuck-investigations": {
            "task": "incidentpilot.escalate_stuck_investigations",
            "schedule": 60.0,
        }
    },
)
