"""Writing audit events. Callers write them explicitly, inside the same transaction as the change.

The request ID, IP address, user agent and (unless given) the actor and school come from the current
request context. ``metadata`` passes through the log redactor and is size-limited, so a careless caller
cannot store a password, token or OTP in the trail.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from typing import Any

from eduflow.core.logging import get_logger, redact
from eduflow.core.request_context import get_request_id, get_request_info

from .models import AuditEvent, Outcome

log = get_logger(__name__)

_MAX_METADATA_BYTES = 4096
_UNSET: Any = object()


def _uuid(value: Any) -> uuid.UUID | None:
    if value is None or value == "":
        return None
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


def _clean_metadata(metadata: Mapping[str, Any] | None) -> dict[str, Any]:
    if not metadata:
        return {}
    cleaned = redact(dict(metadata))
    encoded = json.dumps(cleaned, default=str)
    if len(encoded.encode()) > _MAX_METADATA_BYTES:
        return {"truncated": True}
    return dict(json.loads(encoded))


def record(
    action: str,
    *,
    outcome: str = Outcome.SUCCESS,
    actor_id: Any = _UNSET,
    school_id: Any = _UNSET,
    target_type: str = "",
    target_id: Any = "",
    metadata: Mapping[str, Any] | None = None,
) -> AuditEvent:
    info = get_request_info()
    event = AuditEvent.objects.create(
        action=action,
        outcome=str(outcome),
        actor_id=_uuid(info.user_id if actor_id is _UNSET else actor_id),
        school_id=_uuid(info.school_id if school_id is _UNSET else school_id),
        target_type=target_type,
        target_id=str(target_id or ""),
        request_id=get_request_id() or "",
        ip=info.ip,
        user_agent=info.user_agent,
        metadata=_clean_metadata(metadata),
    )
    log.info("audit_event", action=action, outcome=str(outcome), audit_id=str(event.id))
    return event
