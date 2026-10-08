"""Carry the request ID from the code that enqueues a task into the worker that runs it (ADR-010/012).

* Publishing side: ``before_task_publish`` copies the current request ID into the message headers.
* Worker side: ``task_prerun`` binds that ID, plus the task ID and name, to the logging context.
  ``task_postrun`` restores whatever context was there before, so a task executed inline (eager
  mode, ``apply()``) does not wipe the calling request's context.
* A task with no request ID (e.g. scheduled by beat) inherits the caller's ID when run inline, or
  gets a fresh one, so its log lines still group together.
"""

from __future__ import annotations

from typing import Any

import structlog
from celery import Task
from celery.signals import before_task_publish, task_postrun, task_prerun

from .logging import get_logger
from .request_context import bind_request_id, clear_context, get_request_id, normalize_request_id

HEADER = "request_id"

log = get_logger("eduflow.tasks")

# task_id -> (logging context, request_id) that was active before the task started.
_saved: dict[str, tuple[dict[str, Any], str | None]] = {}


def _request_id_from_task(task: Task[Any, Any]) -> str | None:
    request = task.request
    value = getattr(request, HEADER, None)
    if value is None:
        headers = getattr(request, "headers", None) or {}
        value = headers.get(HEADER)
    return str(value) if value else None


@before_task_publish.connect
def add_request_id_header(headers: dict[str, Any] | None = None, **_: Any) -> None:
    if headers is None:
        return
    request_id = get_request_id()
    if request_id and HEADER not in headers:
        headers[HEADER] = request_id


@task_prerun.connect
def bind_task_context(task_id: str | None = None, task: Task[Any, Any] | None = None, **_: Any) -> None:
    previous_context = dict(structlog.contextvars.get_contextvars())
    previous_request_id = get_request_id()
    if task_id:
        _saved[task_id] = (previous_context, previous_request_id)

    from_message = _request_id_from_task(task) if task is not None else None
    request_id = normalize_request_id(from_message or previous_request_id)

    clear_context()
    bind_request_id(request_id)
    structlog.contextvars.bind_contextvars(task_id=task_id, task_name=getattr(task, "name", None))
    log.info("task_started")


@task_postrun.connect
def clear_task_context(task_id: str | None = None, state: str | None = None, **_: Any) -> None:
    log.info("task_finished", state=state)
    previous_context, previous_request_id = _saved.pop(task_id, ({}, None)) if task_id else ({}, None)
    clear_context()
    if previous_request_id:
        bind_request_id(previous_request_id)
    if previous_context:
        structlog.contextvars.bind_contextvars(**previous_context)
