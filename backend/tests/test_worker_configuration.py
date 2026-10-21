from app.workers.celery_app import celery_app


def test_celery_worker_imports_investigation_task_module() -> None:
    assert "app.workers.tasks" in celery_app.conf.imports


def test_celery_beat_expires_pending_approvals() -> None:
    assert celery_app.conf.beat_schedule["expire-pending-approvals"] == {
        "task": "incidentpilot.expire_pending_approvals",
        "schedule": 60.0,
    }
