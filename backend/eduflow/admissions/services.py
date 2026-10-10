"""Admissions writes: applications, stage moves, offer decisions and enrolment. Transactional and audited
(``admissions.application.*``); every stage change also leaves a ``StageChange`` row."""

from __future__ import annotations

import datetime
from typing import Any

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from eduflow.academics.models import AcademicYear, Grade, RecordStatus, Section
from eduflow.authz.catalog import DataScope
from eduflow.authz.grants import Actor
from eduflow.core.api import Conflict
from eduflow.documents import files
from eduflow.people import services as people
from eduflow.tenancy import domain
from eduflow.tenancy.models import Membership, School

from .models import ORDER, Application, ApplicationDocument, OfferDecision, Source, Stage, StageChange

TERMINAL = {Stage.ENROLLED, Stage.DROPPED}


def _change(app: Application, to_stage: str, by: Membership | None, note: str = "") -> None:
    StageChange.objects.create(
        school_id=app.school_id, application=app, from_stage=app.stage, to_stage=to_stage, note=note, by=by
    )
    app.stage = to_stage


def _refs(school: Any, data: dict[str, Any]) -> dict[str, Any]:
    grade = domain.resolve(Grade, school, data.pop("grade_id"), "grade_id", label="grade")
    if grade.status != RecordStatus.ACTIVE:
        raise ValidationError({"grade_id": ["This grade is archived."]})
    data["grade"] = grade
    year_id = data.pop("academic_year_id", None)
    data["academic_year"] = (
        domain.resolve(AcademicYear, school, year_id, "academic_year_id") if year_id else None
    )
    return data


@transaction.atomic
def create(actor: Actor, **data: Any) -> Application:
    app = Application.objects.create(
        school=actor.school, created_by=actor.membership, **_refs(actor.school, data)
    )
    StageChange.objects.create(
        school=actor.school, application=app, to_stage=Stage.ENQUIRY, by=actor.membership
    )
    domain.record("admissions.application.created", app, source=app.source, grade=str(app.grade_id))
    return app


@transaction.atomic
def apply_online(school: School, **data: Any) -> Application:
    """A public online application: always an enquiry from the website. Staff take it from there."""
    app = Application.objects.create(school=school, source=Source.WEBSITE, **_refs(school, data))
    StageChange.objects.create(
        school=school, application=app, to_stage=Stage.ENQUIRY, note="Online application"
    )
    from eduflow.audit import services as audit

    audit.record(
        "admissions.application.submitted_online",
        actor_id=None,
        school_id=school.pk,
        target_type="application",
        target_id=app.pk,
        metadata={"grade": str(app.grade_id)},
    )
    return app


def _locked(app: Application) -> Application:
    return Application.objects.select_for_update(of=("self",)).get(pk=app.pk)


@transaction.atomic
def update(actor: Actor, app: Application, **data: Any) -> Application:
    app = _locked(app)
    if app.stage in TERMINAL:
        raise Conflict(f"This application is {app.stage}; it cannot be changed.")
    if "grade_id" in data:
        data = _refs(
            actor.school, {"academic_year_id": data.pop("academic_year_id", app.academic_year_id), **data}
        )
    changed = domain.apply_changes(app, data)
    if changed:
        app.save()
        domain.record("admissions.application.updated", app, fields=changed)
    return app


@transaction.atomic
def move(actor: Actor, app: Application, *, stage: str, note: str = "") -> Application:
    app = _locked(app)
    if app.stage in TERMINAL:
        raise Conflict(f"This application is {app.stage}; it cannot move.")
    if stage == Stage.ENROLLED:
        raise ValidationError({"stage": ["Enrol an approved offer with the enrol action."]})
    if stage == Stage.OFFER and app.stage == Stage.OFFER:
        raise Conflict("This application is already at the offer stage.")
    if stage != Stage.DROPPED and ORDER.index(Stage(stage)) <= ORDER.index(Stage(app.stage)):
        raise ValidationError({"stage": [f"An application moves forwards only (it is at {app.stage})."]})
    previous = app.stage
    _change(app, stage, actor.membership, note)
    if stage == Stage.OFFER:
        app.offer_decision = OfferDecision.PENDING
        app.offer_requested_by, app.offer_requested_at = actor.membership, timezone.now()
    app.save()
    domain.record("admissions.application.moved", app, from_stage=previous, to_stage=stage)
    return app


@transaction.atomic
def decide_offer(actor: Actor, app: Application, decision: str, note: str = "") -> Application:
    """Approval queue decision on an offer: approve (it may be enrolled) or decline (it drops)."""
    app = _locked(app)
    if app.stage != Stage.OFFER or app.offer_decision != OfferDecision.PENDING:
        raise Conflict("Only a pending offer can be decided.")
    app.offer_decided_by, app.offer_decided_at = actor.membership, timezone.now()
    if decision == "approve":
        app.offer_decision = OfferDecision.APPROVED
    else:
        app.offer_decision = OfferDecision.DECLINED
        _change(app, Stage.DROPPED, actor.membership, note or "Offer declined")
    app.save()
    domain.record("admissions.application.offer_decided", app, decision=app.offer_decision, note=note or None)
    return app


def _school_wide(actor: Actor, *permissions: str) -> None:
    if not all(DataScope.SCHOOL in actor.scopes(p) for p in permissions):
        raise PermissionDenied("Enrolling a student needs student, guardian and enrollment permissions.")


@transaction.atomic
def enrol(
    actor: Actor,
    app: Application,
    *,
    section_id: Any,
    admission_number: str,
    start_date: datetime.date | None = None,
    roll_number: str = "",
) -> Application:
    """Turn an approved offer into a student, a guardian, their link and an enrollment (people services)."""
    _school_wide(actor, "student.create", "guardian.manage", "enrollment.manage")
    app = _locked(app)
    if app.stage != Stage.OFFER or app.offer_decision != OfferDecision.APPROVED:
        raise Conflict("Only an approved offer can be enrolled.")
    section = domain.resolve(Section, actor.school, section_id, "section_id", label="section")
    if section.grade_id != app.grade_id:
        raise ValidationError({"section_id": ["This section is not in the applied-for grade."]})
    first, _, last = app.child_name.partition(" ")
    student = people.create_student(
        actor,
        admission_number=admission_number,
        first_name=first,
        last_name=last,
        date_of_birth=app.date_of_birth,
        admission_date=start_date or timezone.localdate(),
    )
    guardian = people.create_guardian(actor, full_name=app.parent_name, phone=app.phone, email=app.email)
    people.link_guardian(
        actor, student_id=student.pk, guardian_id=guardian.pk, relationship="guardian", is_primary=True
    )
    data: dict[str, Any] = {"roll_number": roll_number}
    if start_date:
        data["start_date"] = start_date
    enrollment = people.enroll(actor, student_id=student.pk, section_id=section.pk, **data)
    app.student, app.guardian, app.enrollment = student, guardian, enrollment
    _change(app, Stage.ENROLLED, actor.membership)
    app.save()
    domain.record("admissions.application.enrolled", app, student=str(student.pk), section=str(section.pk))
    return app


@transaction.atomic
def attach(actor: Actor, app: Application, *, upload: Any, title: str) -> ApplicationDocument:
    stored = files.store(actor.school.pk, upload, actor.membership)
    doc = ApplicationDocument.objects.create(school=actor.school, application=app, title=title, file=stored)
    domain.record(
        "admissions.application.document_added", app, document=str(doc.pk), content_type=stored.content_type
    )
    return doc
