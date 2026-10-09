"""Tenant resolution: from the ``X-School-Id`` header to a verified :class:`~eduflow.authz.grants.Actor`.

The header is only a *request* to act in a school. It is honoured only if the authenticated user has an
active membership in an active school with that ID. A malformed ID, an unknown school, an inactive school
and a school the user does not belong to all produce the same ``403 tenant_forbidden``, so the endpoint
reveals nothing about which schools exist. Platform administrators get no implicit school access (ADR-003).

Once resolved, the school is written to the database context, so RLS policies enforce the same boundary.

**Host binding (white-label, ADR-029).** A request made on a school's own host (its platform subdomain or a
verified custom domain) can only act in that school: without ``X-School-Id`` the host's school is used, and a
header naming any other school is the same ``403 tenant_forbidden``. The host only *selects* the school; the
membership check below is unchanged. Hosts are matched by ``TENANT_HOST_RESOLVER`` after Django has validated
them against ``ALLOWED_HOSTS``.
"""

from __future__ import annotations

import uuid

from django.conf import settings
from django.utils.module_loading import import_string
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


def host_school(request: Request) -> uuid.UUID | None:
    """The school the request's own Host belongs to, or None (a platform or unknown host)."""
    path = getattr(settings, "TENANT_HOST_RESOLVER", "")
    if not path:
        return None
    resolver = import_string(path)
    school: uuid.UUID | None = resolver(request.get_host())
    return school


def _denied(target: object) -> TenantForbidden:
    log.warning("tenant_forbidden")
    audit.record(
        "tenancy.access_denied",
        outcome=Outcome.DENIED,
        school_id=None,
        target_type="school",
        target_id=target,
    )
    return TenantForbidden()


def resolve_actor(request: Request) -> Actor:
    raw = request.headers.get(SCHOOL_HEADER, "").strip()
    bound = host_school(request)
    if not raw and bound is None:
        raise TenantRequired()
    try:
        school_id = uuid.UUID(raw) if raw else bound
    except ValueError:
        raise TenantForbidden() from None
    if school_id is None:  # pragma: no cover - excluded by the first check
        raise TenantRequired()
    if bound is not None and school_id != bound:
        raise _denied(school_id)  # a school's host never acts for another school

    membership = (
        Membership.objects.select_related("school")
        .filter(user=request_user(request), school_id=school_id, is_active=True, school__is_active=True)
        .first()
    )
    if membership is None:
        raise _denied(school_id)

    db_context.update(school_id=school_id)
    bind_request_info(school_id=str(school_id))
    return Actor(
        user=request_user(request),
        school=membership.school,
        membership=membership,
        grants=grants_for(membership, membership.school),
    )
