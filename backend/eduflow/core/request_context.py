"""Request / correlation identifiers shared by HTTP requests, logs and background jobs.

One ``request_id`` follows a unit of work from the client, through the API, into the log lines it
produces and into any Celery task it enqueues (see ``eduflow.core.celery_context``). It lives in a
contextvar so it is correct under threads and async alike.
"""

from __future__ import annotations

import re
import uuid
from contextvars import ContextVar

import structlog

REQUEST_ID_HEADER = "X-Request-ID"
# META key Django uses for the header above.
REQUEST_ID_META_KEY = "HTTP_X_REQUEST_ID"

# Accept a caller-supplied ID only if it is short and made of safe characters. Anything else is
# replaced, so a client cannot inject newlines or huge values into our logs.
_VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")

_request_id: ContextVar[str | None] = ContextVar("request_id", default=None)


def new_request_id() -> str:
    return uuid.uuid4().hex


def normalize_request_id(candidate: str | None) -> str:
    """Return ``candidate`` if it is a safe identifier, else a freshly generated one."""
    if candidate and _VALID_REQUEST_ID.fullmatch(candidate):
        return candidate
    return new_request_id()


def get_request_id() -> str | None:
    return _request_id.get()


def bind_request_id(request_id: str) -> None:
    """Bind ``request_id`` for the current context, both for code and for log lines."""
    _request_id.set(request_id)
    structlog.contextvars.bind_contextvars(request_id=request_id)


def clear_context() -> None:
    _request_id.set(None)
    structlog.contextvars.clear_contextvars()
