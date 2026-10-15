"""Celery application bootstrap.

This module intentionally contains **no tasks** - it only wires the broker,
result backend and process-level defaults so the worker container in
``docker-compose.yml`` can boot. Task modules are declared next step and are
discovered through the ``include`` list below.

Start a worker with::

    celery -A app.tasks.celery_app:celery_app worker --loglevel=INFO
"""

from __future__ import annotations

from celery import Celery

from app.core.config import settings

#: Task modules imported by the worker on startup. Every new module must be
#: added here, otherwise the worker will not register its tasks. The worker
#: imports these; the API never does, which is what keeps torch out of the API
#: image (see the Dockerfile's two targets).
TASK_MODULES: tuple[str, ...] = ("app.tasks.image_processing",)

#: Canonical task name. The API dispatches with ``send_task(PROCESS_IMAGES_TASK)``
#: rather than importing the function, so importing the task - and therefore
#: ultralytics/torch - stays confined to the worker process.
PROCESS_IMAGES_TASK = "app.tasks.image_processing.process_images_task"

#: Reconstructs a project's 3D model from the frames it already holds. Separate
#: from the analysis task because it is requested on its own: the frames of a
#: project whose reconstruction never finished are `completed`, so the analysis
#: task has nothing to claim and would return before reaching its 3D phase.
BUILD_MODEL3D_TASK = "app.tasks.image_processing.build_model3d_task"

celery_app = Celery(
    "facade_inspection",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=list(TASK_MODULES),
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],  # never unpickle untrusted payloads
    timezone="UTC",
    enable_utc=True,
    # Redelivery safety: a killed worker returns its task to the queue instead
    # of silently dropping a batch of drone images.
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,  # long, unequal AI tasks
    worker_max_tasks_per_child=50,  # cap memory growth from the YOLO runtime
    result_expires=86_400,
    task_always_eager=settings.CELERY_TASK_ALWAYS_EAGER,
    task_track_started=True,
    broker_connection_retry_on_startup=True,
)

__all__ = ["celery_app", "TASK_MODULES", "PROCESS_IMAGES_TASK", "BUILD_MODEL3D_TASK"]
