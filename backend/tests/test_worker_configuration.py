from app.workers.celery_app import celery_app


def test_celery_worker_imports_investigation_task_module() -> None:
    assert "app.workers.tasks" in celery_app.conf.imports
