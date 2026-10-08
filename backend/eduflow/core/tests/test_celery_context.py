"""Request-ID propagation into background jobs.

The signal handlers are tested directly, because eager mode (used in unit tests) does not publish
messages. The real broker round trip is verified by the Docker/CI smoke test (docs/deployment/local-setup.md).
"""

from types import SimpleNamespace
from typing import Any

import pytest

from eduflow.core import celery_context
from eduflow.core.logging import get_logger
from eduflow.core.request_context import bind_request_id, clear_context, get_request_id
from eduflow.core.tasks import ping

# The task signals switch the database role (RLS context), so they need database access.
pytestmark = pytest.mark.django_db


def _as_task(fake: SimpleNamespace) -> Any:
    """A stand-in with the attributes the signal handlers read (name, request)."""
    return fake


def test_publish_copies_current_request_id_into_headers():
    bind_request_id("req-publish-0001")
    headers: dict[str, str] = {}
    celery_context.add_request_id_header(headers=headers)
    assert headers == {"request_id": "req-publish-0001"}
    clear_context()


def test_publish_without_request_adds_nothing():
    clear_context()
    headers: dict[str, str] = {}
    celery_context.add_request_id_header(headers=headers)
    assert headers == {}


def test_worker_binds_request_id_from_message(json_logs):
    task = SimpleNamespace(
        name="eduflow.core.tasks.ping", request=SimpleNamespace(request_id="req-from-api-42")
    )
    celery_context.bind_task_context(task_id="task-1", task=_as_task(task))
    assert get_request_id() == "req-from-api-42"
    get_logger("test").info("inside_task")
    celery_context.clear_task_context(state="SUCCESS")
    assert get_request_id() is None

    [line] = json_logs.find("inside_task")
    assert line["request_id"] == "req-from-api-42"
    assert line["task_id"] == "task-1"
    assert line["task_name"] == "eduflow.core.tasks.ping"


def test_worker_reads_request_id_from_headers_mapping():
    task = SimpleNamespace(name="t", request=SimpleNamespace(headers={"request_id": "req-in-headers-7"}))
    celery_context.bind_task_context(task_id="task-2", task=_as_task(task))
    assert get_request_id() == "req-in-headers-7"
    celery_context.clear_task_context()


def test_task_without_request_gets_fresh_id():
    task = SimpleNamespace(name="t", request=SimpleNamespace())
    celery_context.bind_task_context(task_id="task-3", task=_as_task(task))
    assert get_request_id()
    celery_context.clear_task_context()


def test_ping_task_runs_inline_and_keeps_caller_context():
    bind_request_id("req-eager-ping-1")
    try:
        result = ping.delay().get(timeout=5)
        # The calling request's context survives the inline task.
        assert get_request_id() == "req-eager-ping-1"
    finally:
        clear_context()
    assert result["pong"] is True
    assert result["request_id"] == "req-eager-ping-1"
