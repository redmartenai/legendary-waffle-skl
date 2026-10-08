"""Structured logging (ADR-012).

Every log line, from our code, Django, Celery or a library, goes through one structlog pipeline:

    contextvars (request_id, task_id, ...) -> redaction -> timestamp -> JSON (or console in dev)

Redaction runs on every event, so a careless ``log.info("x", password=...)`` or a library that logs
headers cannot leak secrets. The redaction rules are deliberately conservative: values are masked
by key name, and token-shaped strings are masked wherever they appear.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping, MutableMapping
from typing import Any

import structlog
from structlog.typing import EventDict, Processor, WrappedLogger

REDACTED = "[REDACTED]"

# Keys whose values are always masked (case-insensitive, matched as a substring).
_SENSITIVE_KEY_PARTS = (
    "password",
    "passwd",
    "secret",
    "token",
    "authorization",
    "cookie",
    "otp",
    "api_key",
    "apikey",
    "private_key",
    "sessionid",
    "csrf",
    "credential",
    "signature",
)
# Keys masked only on exact match, because as substrings they are too common (e.g. "accessed_at").
_SENSITIVE_EXACT_KEYS = frozenset({"access", "refresh", "code", "dev_code", "pin", "key"})

# Values that look like credentials wherever they appear in a string.
_VALUE_PATTERNS = (
    re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]*"),  # JWT
    re.compile(r"(?i)(://[^/\s:@]+:)[^@\s/]+(@)"),  # credentials in URLs: scheme://user:pass@host
    # Inline "password=..." / "token: ..." in free text, e.g. inside exception messages.
    re.compile(r"(?i)(\b(?:password|passwd|secret|token|api[_-]?key|otp)\s*[=:]\s*)[^\s,;&\"']+()"),
)

# Never descend further than this. Deeply nested structures are truncated instead of masked.
_MAX_DEPTH = 8


def _is_sensitive_key(key: object) -> bool:
    if not isinstance(key, str):
        return False
    lowered = key.lower().replace("-", "_")
    return lowered in _SENSITIVE_EXACT_KEYS or any(part in lowered for part in _SENSITIVE_KEY_PARTS)


def _scrub_string(value: str) -> str:
    for pattern in _VALUE_PATTERNS:
        if pattern.groups == 2:
            value = pattern.sub(rf"\g<1>{REDACTED}\g<2>", value)
        else:
            value = pattern.sub(REDACTED, value)
    return value


def redact(value: Any, depth: int = 0) -> Any:
    """Return a copy of ``value`` with sensitive keys and token-shaped strings masked."""
    if depth > _MAX_DEPTH:
        return "[TRUNCATED]"
    if isinstance(value, str):
        return _scrub_string(value)
    if isinstance(value, Mapping):
        return {k: (REDACTED if _is_sensitive_key(k) else redact(v, depth + 1)) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [redact(v, depth + 1) for v in value]
    return value


def redact_processor(_logger: WrappedLogger, _method: str, event_dict: EventDict) -> EventDict:
    """structlog processor: mask secrets in every field, including the event message."""
    for key in list(event_dict.keys()):
        if key in ("timestamp", "level", "logger"):
            continue
        event_dict[key] = REDACTED if _is_sensitive_key(key) else redact(event_dict[key])
    return event_dict


def _drop_color_message(_logger: WrappedLogger, _method: str, event_dict: EventDict) -> EventDict:
    # uvicorn/gunicorn duplicate the message with ANSI colours, and ExtraAdder can copy the stdlib
    # record's formatted "message" next to "event". Both are noise.
    event_dict.pop("color_message", None)
    if event_dict.get("message") == event_dict.get("event"):
        event_dict.pop("message", None)
    return event_dict


def _shared_processors() -> list[Processor]:
    return [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.ExtraAdder(),
        _drop_color_message,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        # Redact before exceptions are formatted, and again after (format_exc_info below), so
        # secrets inside exception messages are also masked.
        redact_processor,
    ]


def build_logging_config(level: str = "INFO", fmt: str = "json") -> dict[str, Any]:
    """Configure structlog and return a ``LOGGING`` dict for Django's dictConfig."""
    renderer: Processor
    if fmt == "console":
        renderer = structlog.dev.ConsoleRenderer(colors=False)
    else:
        renderer = structlog.processors.JSONRenderer()

    shared = _shared_processors()

    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            *shared,
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "structured": {
                "()": structlog.stdlib.ProcessorFormatter,
                "foreign_pre_chain": shared,
                "processors": [
                    structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                    structlog.processors.format_exc_info,
                    redact_processor,
                    renderer,
                ],
            }
        },
        "handlers": {
            "stdout": {
                "class": "logging.StreamHandler",
                "formatter": "structured",
                "stream": "ext://sys.stdout",
            }
        },
        "root": {"handlers": ["stdout"], "level": level},
        "loggers": {
            # Our access log replaces Django's request logging. Keep django.request for 5xx tracebacks.
            "django.server": {"level": "WARNING"},
            "django.db.backends": {"level": "WARNING"},
            "celery": {"level": level},
            "botocore": {"level": "WARNING"},
            "urllib3": {"level": "WARNING"},
        },
    }


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.stdlib.get_logger(name)


def bound_context() -> MutableMapping[str, Any]:
    """The currently bound contextvars (request_id, task_id, ...)."""
    return structlog.contextvars.get_contextvars()


__all__ = ["REDACTED", "bound_context", "build_logging_config", "get_logger", "redact", "redact_processor"]

logging.captureWarnings(True)
