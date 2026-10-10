"""Visitor writes. Transactional and audited (``visitors.*``). Pass tokens are never stored or logged."""

from __future__ import annotations

import datetime
import hashlib
import secrets
from typing import Any

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from eduflow.authz.catalog import DataScope
from eduflow.authz.grants import Actor
from eduflow.core.api import Conflict
from eduflow.notifications import services as notifications
from eduflow.people import policies as people_policies
from eduflow.tenancy import clock, domain
from eduflow.tenancy.models import Membership

from .models import Visit, VisitStatus

MANAGE = "visitor.manage"


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _issue_pass(visit: Visit) -> str:
    token = secrets.token_urlsafe(24)
    visit.pass_hash = _hash(token)
    return token


def _is_security(actor: Actor) -> bool:
    return DataScope.SCHOOL in actor.scopes(MANAGE)


@transaction.atomic
def register(
    actor: Actor,
    *,
    visitor_name: str,
    phone: str,
    purpose: str,
    expected_on: datetime.date | None = None,
    host_id: Any = None,
    student_id: Any = None,
    visitors_count: int = 1,
    vehicle_number: str = "",
    id_proof: str = "",
) -> tuple[Visit, str | None]:
    """Security registers at the gate (approved at once, a pass is returned); anyone else pre-registers a
    visit they host (it waits for security)."""
    today = clock.today(actor.school)
    expected_on = expected_on or today
    if expected_on < today:
        raise ValidationError({"expected_on": ["The visit date is in the past."]})
    security = _is_security(actor)
    host: Membership | None
    if host_id and security:
        host = domain.resolve(Membership, actor.school, host_id, "host_id", label="member")
    else:
        host = actor.membership if not security else None
    student = None
    if student_id:
        student = people_policies.students.queryset(actor, "visitor.register").filter(pk=student_id).first()
        if student is None:
            raise ValidationError({"student_id": ["Unknown student."]})
    visit = Visit(
        school=actor.school,
        visitor_name=visitor_name,
        phone=phone,
        purpose=purpose,
        expected_on=expected_on,
        host=host,
        student=student,
        visitors_count=visitors_count,
        vehicle_number=vehicle_number,
        id_proof=id_proof,
        registered_by=actor.membership,
    )
    token = None
    if security:
        visit.status, visit.decided_by, visit.decided_at = (
            VisitStatus.APPROVED,
            actor.membership,
            timezone.now(),
        )
        token = _issue_pass(visit)
    visit.save()
    domain.record("visitors.visit.registered", visit, at_gate=security or None)
    return visit, token


@transaction.atomic
def decide(actor: Actor, visit: Visit, *, decision: str, note: str = "") -> tuple[Visit, str | None]:
    visit = Visit.objects.select_for_update().get(pk=visit.pk)
    if visit.status != VisitStatus.PENDING:
        raise Conflict("This visit has already been decided.")
    visit.decided_by, visit.decided_at, visit.decision_note = actor.membership, timezone.now(), note
    token = None
    if decision == "approve":
        visit.status = VisitStatus.APPROVED
        token = _issue_pass(visit)
    else:
        visit.status = VisitStatus.DECLINED
    visit.save()
    domain.record("visitors.visit.decided", visit, decision=visit.status)
    if visit.host is not None:
        notifications.notify(
            actor.school,
            [visit.host],
            kind="approval",
            title=f"Visit by {visit.visitor_name} {visit.status}",
            body=note,
            link=("visit", visit.pk),
        )
    return visit, token


@transaction.atomic
def reissue_pass(actor: Actor, visit: Visit) -> str:
    """A new pass for an approved visit (the host shares it with the visitor). The old pass stops working."""
    if not _is_security(actor) and visit.host_id != actor.membership.pk:
        raise PermissionDenied("Only the host or security can issue a pass.")
    visit = Visit.objects.select_for_update().get(pk=visit.pk)
    if visit.status != VisitStatus.APPROVED:
        raise Conflict("A pass is issued only for an approved visit that has not started.")
    token = _issue_pass(visit)
    visit.save(update_fields=["pass_hash"])
    domain.record("visitors.visit.pass_issued", visit)
    return token


@transaction.atomic
def scan(actor: Actor, *, token: str) -> Visit:
    """The gate scans a pass: check in, or check out if the visitor is inside."""
    visit = (
        Visit.objects.select_for_update().filter(school_id=actor.school.pk, pass_hash=_hash(token)).first()
    )
    if visit is None:
        raise NotFound("This pass is not valid.")
    now = timezone.now()
    if visit.status == VisitStatus.APPROVED:
        if visit.expected_on != clock.today(actor.school):
            raise Conflict(f"This pass is for {visit.expected_on.isoformat()}.")
        visit.status, visit.checked_in_at = VisitStatus.INSIDE, now
    elif visit.status == VisitStatus.INSIDE:
        visit.status, visit.checked_out_at = VisitStatus.LEFT, now
        visit.pass_hash = ""  # a used pass never works again
    else:
        raise Conflict(f"This visit is {visit.get_status_display().lower()}.")
    visit.save()
    domain.record("visitors.visit.scanned", visit, status=visit.status)
    if visit.status == VisitStatus.INSIDE and visit.host is not None:
        notifications.notify(
            actor.school,
            [visit.host],
            kind="message",
            title=f"{visit.visitor_name} has arrived",
            link=("visit", visit.pk),
        )
    return visit


@transaction.atomic
def cancel(actor: Actor, visit: Visit) -> Visit:
    if not _is_security(actor) and actor.membership.pk not in (visit.host_id, visit.registered_by_id):
        raise PermissionDenied()
    visit = Visit.objects.select_for_update().get(pk=visit.pk)
    if visit.status not in (VisitStatus.PENDING, VisitStatus.APPROVED):
        raise Conflict("This visit can no longer be cancelled.")
    visit.status, visit.pass_hash = VisitStatus.CANCELLED, ""
    visit.save()
    domain.record("visitors.visit.cancelled", visit)
    return visit


def inside_now(school: Any) -> list[Visit]:
    return list(
        Visit.objects.filter(school_id=school.pk, status=VisitStatus.INSIDE).order_by("checked_in_at")
    )
