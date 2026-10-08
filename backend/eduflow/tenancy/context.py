"""Tenant resolution: from the ``X-School-Id`` header to a verified :class:`~eduflow.authz.grants.Actor`.

The header is only a *request* to act in a school. It is honoured only if the authenticated user has an
active membership in an active school with that ID. A malformed ID, an unknown school, an inactive school
and a school the user does not belong to all produce the same ``403 tenant_forbidden``, so the endpoint
reveals nothing about which schools exist. Platform administrators get no implicit school access (ADR-003).

Once resolved, the school is written to the database context, so RLS policies enforce the same boundary.
"""

from __future__ import annotations

import uuid

from rest_framework.request import Request

from eduflow.audit import services as audit
from eduflow.audit.models import Outcome
from eduflow.authz.grants import Actor, grants_for
from eduflow.core import db_context
from eduflow.core.api import TenantForbidden, TenantRequired
from eduflow.core.logging import get_logger
from eduflow.core.request_context import bind_request_info
from eduflow.identity.authentication import request_user

from .models import Membership

SCHOOL_HEADER = "X-School-Id"

log = get_logger(__name__)


def resolve_actor(request: Request) -> Actor:
    raw = request.headers.get(SCHOOL_HEADER, "").strip()
    if not raw:
        raise TenantRequired()
    try:
        school_id = uuid.UUID(raw)
    except ValueError:
        raise TenantForbidden() from None

    membership = (
        Membership.objects.select_related("school")
        .filter(user=request_user(request), school_id=school_id, is_active=True, school__is_active=True)
        .first()
    )
    if membership is None:
        log.warning("tenant_forbidden")
        audit.record(
            "tenancy.access_denied",
            outcome=Outcome.DENIED,
            school_id=None,
            target_type="school",
            target_id=school_id,
        )
        raise TenantForbidden()

    db_context.update(school_id=school_id)
    bind_request_info(school_id=str(school_id))
    return Actor(
        user=request_user(request),
        school=membership.school,
        membership=membership,
        grants=grants_for(membership, membership.school),
    )
