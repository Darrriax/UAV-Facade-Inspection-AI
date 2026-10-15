"""Asynchronous task package (Celery).

Importing task modules here would create a cycle (the task imports the Celery
app), so only the app instance is re-exported; tasks live in their own modules
and are registered through ``celery_app.TASK_MODULES``.
"""

from app.tasks.celery_app import celery_app

__all__ = ["celery_app"]
