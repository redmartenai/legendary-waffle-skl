"""Document uploads, archiving and audited downloads.

* School-wide ``document.manage``: any document, any audience.
* A teacher (``document.manage`` with ``section``) may upload a document **about a student they teach**,
  for the audiences ``teachers``, ``class_teacher``, ``parents``, ``students`` or ``principal``. The student
  is loaded through the caller's scope and the service re-checks the relationship (ADR-027).
* Archiving needs the school-wide grant. Every download is audited (screen documentation: "document
  downloads should be audited").
"""

from __future__ import annotations

from typing import Any

from django.db import transaction
from django.http import HttpResponse
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from eduflow.authz.catalog import DataScope
from eduflow.authz.grants import Actor
from eduflow.core.api import Conflict
from eduflow.people import policies as people_policies
from eduflow.people.models import StaffProfile, Student, TeacherAssignment
from eduflow.tenancy import domain

from . import files
from .models import Audience, Document

TEACHER_AUDIENCES = {
    Audience.TEACHERS,
    Audience.CLASS_TEACHER,
    Audience.PARENTS,
    Audience.STUDENTS,
    Audience.PRINCIPAL,
}


def _school_wide(actor: Actor, permission: str) -> bool:
    return DataScope.SCHOOL in actor.scopes(permission)


@transaction.atomic
def upload(
    actor: Actor,
    *,
    upload_file: Any,
    title: str,
    audiences: list[str],
    category: str = "",
    student_id: Any = None,
    staff_id: Any = None,
) -> Document:
    if student_id and staff_id:
        raise ValidationError(
            {"student_id": ["A document is about one student or one staff member, not both."]}
        )
    if not audiences:
        raise ValidationError({"audiences": ["Choose at least one audience."]})
    student = staff = None
    if _school_wide(actor, "document.manage"):
        if student_id:
            student = domain.resolve(Student, actor.school, student_id, "student_id", label="student")
        if staff_id:
            staff = domain.resolve(StaffProfile, actor.school, staff_id, "staff_id", label="staff member")
    else:
        if not student_id or staff_id:
            raise PermissionDenied("Teachers upload documents about a student they teach.")
        student = people_policies.students.get(actor, "document.manage", student_id)
        if not TeacherAssignment.objects.filter(
            staff__membership=actor.membership,
            status="active",
            section__enrollments__student=student,
            section__enrollments__status="active",
        ).exists():
            raise PermissionDenied("Teachers upload documents about a student they teach.")
        if not set(audiences) <= TEACHER_AUDIENCES:
            raise ValidationError({"audiences": ["Teachers cannot share with these audiences."]})
    stored = files.store(actor.school.pk, upload_file, actor.membership)
    document = Document.objects.create(
        school=actor.school,
        title=title,
        category=category,
        file=stored,
        student=student,
        staff=staff,
        audiences=sorted(set(audiences)),
        uploaded_by=actor.membership,
    )
    domain.record(
        "documents.document.uploaded",
        document,
        content_type=stored.content_type,
        size=stored.size,
        audiences=document.audiences,
        student=str(student.pk) if student else None,
        staff=str(staff.pk) if staff else None,
    )
    return document


@transaction.atomic
def archive(actor: Actor, document: Document) -> Document:
    if document.archived_at is not None:
        raise Conflict("This document is already archived.")
    document.archived_at = timezone.now()
    document.save(update_fields=["archived_at"])
    domain.record("documents.document.archived", document)
    return document


def download(actor: Actor, document: Document) -> HttpResponse:
    domain.record("documents.document.downloaded", document, by=str(actor.membership.pk))
    return files.download(document.file)
