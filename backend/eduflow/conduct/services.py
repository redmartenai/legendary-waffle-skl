"""Remark and incident writes: narrow (a teacher writes about students they teach), transactional, audited
(``conduct.*``). Families are notified of what they may see."""

from __future__ import annotations

import datetime
from typing import Any

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from eduflow.academics.models import Subject, Term
from eduflow.authz.grants import Actor
from eduflow.core.api import Conflict
from eduflow.notifications import services as notifications
from eduflow.people.models import Student
from eduflow.people.scoping import may_write_for_student
from eduflow.tenancy import clock, domain

from .models import Incident, IncidentStatus, Remark

REMARK, INCIDENT = "remark.manage", "behaviour.manage"


def _student(actor: Actor, student_id: Any, permission: str) -> Student:
    student = domain.resolve(Student, actor.school, student_id, "student_id", label="student")
    if not may_write_for_student(actor, permission, student):
        raise PermissionDenied("You can write only about students you teach.")
    return student


def _guard(actor: Actor, permission: str, obj: Remark | Incident) -> None:
    if not may_write_for_student(actor, permission, obj.student):
        raise PermissionDenied()


@transaction.atomic
def add_remark(
    actor: Actor,
    *,
    student_id: Any,
    tone: str,
    text: str,
    subject_id: Any = None,
    visible_to_family: bool = True,
) -> Remark:
    student = _student(actor, student_id, REMARK)
    subject = domain.resolve(Subject, actor.school, subject_id, "subject_id") if subject_id else None
    remark = Remark.objects.create(
        school=actor.school,
        student=student,
        subject=subject,
        tone=tone,
        text=text,
        visible_to_family=visible_to_family,
        author=actor.membership,
    )
    domain.record("conduct.remark.created", remark, student=str(student.pk), tone=tone)
    if visible_to_family:
        notifications.notify(
            actor.school,
            notifications.family_of(student),
            kind="remark",
            title=f"New {tone} remark",
            body=text[:200],
            student=student,
            link=("remark", remark.pk),
        )
    return remark


@transaction.atomic
def update_remark(actor: Actor, remark: Remark, **data: Any) -> Remark:
    _guard(actor, REMARK, remark)
    changed = domain.apply_changes(remark, data)
    if changed:
        remark.save()
        domain.record("conduct.remark.updated", remark, fields=changed)
    return remark


@transaction.atomic
def delete_remark(actor: Actor, remark: Remark) -> None:
    _guard(actor, REMARK, remark)
    domain.record("conduct.remark.deleted", remark, student=str(remark.student_id))
    remark.delete()


@transaction.atomic
def report_incident(
    actor: Actor,
    *,
    student_id: Any,
    occurred_on: datetime.date,
    category: str,
    description: str,
    severity: str,
    action_taken: str = "",
    visible_to_family: bool = False,
) -> Incident:
    student = _student(actor, student_id, INCIDENT)
    if occurred_on > clock.today(actor.school):
        raise ValidationError({"occurred_on": ["An incident cannot be in the future."]})
    incident = Incident.objects.create(
        school=actor.school,
        student=student,
        occurred_on=occurred_on,
        category=category,
        description=description,
        severity=severity,
        action_taken=action_taken,
        visible_to_family=visible_to_family,
        reported_by=actor.membership,
    )
    domain.record("conduct.incident.reported", incident, student=str(student.pk), severity=severity)
    if visible_to_family:
        notifications.notify(
            actor.school,
            notifications.family_of(student),
            kind="remark",
            title=f"Behaviour incident: {category}",
            body=description[:200],
            student=student,
            link=("incident", incident.pk),
        )
    return incident


@transaction.atomic
def update_incident(actor: Actor, incident: Incident, **data: Any) -> Incident:
    _guard(actor, INCIDENT, incident)
    changed = domain.apply_changes(incident, data)
    if changed:
        incident.save()
        domain.record("conduct.incident.updated", incident, fields=changed)
    return incident


@transaction.atomic
def resolve_incident(actor: Actor, incident: Incident, *, action_taken: str = "") -> Incident:
    _guard(actor, INCIDENT, incident)
    incident = Incident.objects.select_for_update().get(pk=incident.pk)
    if incident.status == IncidentStatus.RESOLVED:
        raise Conflict("This incident is already resolved.")
    incident.status, incident.resolved_by, incident.resolved_at = (
        IncidentStatus.RESOLVED,
        actor.membership,
        timezone.now(),
    )
    if action_taken:
        incident.action_taken = action_taken
    incident.save()
    domain.record("conduct.incident.resolved", incident)
    return incident


def current_term(school: Any) -> Term | None:
    today = clock.today(school)
    return Term.objects.filter(school_id=school.pk, start_date__lte=today, end_date__gte=today).first()


def incident_counts(school: Any, since: datetime.date, until: datetime.date) -> dict[Any, int]:
    """Per student: incidents in ``[since, until]`` (monitoring rule "Repeated behaviour notes")."""
    counts: dict[Any, int] = {}
    rows = Incident.objects.filter(school_id=school.pk, occurred_on__gte=since, occurred_on__lte=until)
    for student_id in rows.values_list("student_id", flat=True):
        counts[student_id] = counts.get(student_id, 0) + 1
    return counts
