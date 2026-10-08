"""Infrastructure tasks only. Domain tasks live in their own modules."""

from __future__ import annotations

from celery import shared_task

from .logging import get_logger
from .request_context import get_request_id

log = get_logger(__name__)


@shared_task(name="eduflow.core.tasks.ping", acks_late=True)
def ping() -> dict[str, object]:
    """Round-trip check for the worker. It echoes the request ID the task ran under."""
    request_id = get_request_id()
    log.info("ping_received")
    return {"pong": True, "request_id": request_id}


@shared_task(name="eduflow.core.tasks.heartbeat", ignore_result=True)
def heartbeat() -> None:
    """Scheduled by beat, so its log line proves that beat and worker are both alive."""
    log.info("heartbeat")
