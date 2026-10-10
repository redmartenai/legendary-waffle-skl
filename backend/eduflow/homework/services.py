"""Homework writes. Transactional and audited (``homework.*``); families are notified in-app."""

from __future__ import annotations

import datetime
from decimal import Decimal
from typing import Any

from django.db import transaction
from django.db.models import QuerySet
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from eduflow.academics.models import Section, Subject
from eduflow.authz.grants import Actor
from eduflow.core.api import Conflict
from eduflow.documents import files
from eduflow.notifications import services as notifications
from eduflow.people.models import Enrollment, StaffProfile, Student
from eduflow.people.scoping import may_write
from eduflow.tenancy import clock, domain

from .models import Homework, HomeworkStatus, Submission, SubmissionStatus

MANAGE = "homework.manage"
REVIEW = "homework.review"


def roster(homework: Homework) -> QuerySet[Student]:
    """Students actively enrolled in the section the homework was set for."""
    return Student.objects.filter(
        school_id=homework.school_id, enrollments__section=homework.section, enrollments__status="active"
    ).distinct()


def _family(students: Any) -> list[Any]:
    members: list[Any] = []
    for student in students:
        members += notifications.family_of(student)
    return members


@transaction.atomic
def create(
    actor: Actor,
    *,
    section_id: Any,
    subject_id: Any,
    title: str,
    due_date: datetime.date,
    description: str = "",
    max_score: Decimal | None = None,
    attachment: Any = None,
) -> Homework:
    section = domain.resolve(Section, actor.school, section_id, "section_id", label="section")
    subject = domain.resolve(Subject, actor.school, subject_id, "subject_id", label="subject")
    if not may_write(actor, MANAGE, section, subject):
        raise PermissionDenied("You can set homework only for a section and subject you teach.")
    today = clock.today(actor.school)
    if due_date < today:
        raise ValidationError({"due_date": ["The due date is in the past."]})
    stored = files.store(actor.school.pk, attachment, actor.membership) if attachment else None
    homework = Homework.objects.create(
        school=actor.school,
        section=section,
        subject=subject,
        teacher=StaffProfile.objects.filter(membership=actor.membership).first(),
        title=title,
        description=description,
        assigned_on=today,
        due_date=due_date,
        max_score=max_score,
        attachment=stored,
        created_by=actor.membership,
    )
    domain.record("homework.homework.created", homework, section=str(section.pk), subject=str(subject.pk))
    notifications.notify(
        actor.school,
        _family(roster(homework)),
        kind="homework",
        title=f"New homework: {title}",
        body=f"{subject.name}, due {due_date.isoformat()}",
        link=("homework", homework.pk),
    )
    return homework


def _check_owner(actor: Actor, homework: Homework) -> None:
    if not may_write(actor, MANAGE, homework.section, homework.subject):
        raise PermissionDenied()


@transaction.atomic
def update(actor: Actor, homework: Homework, **data: Any) -> Homework:
    _check_owner(actor, homework)
    if homework.status == HomeworkStatus.ARCHIVED:
        raise Conflict("This homework is archived.")
    if "due_date" in data and data["due_date"] < homework.assigned_on:
        raise ValidationError({"due_date": ["The due date is before the day it was set."]})
    changed = domain.apply_changes(homework, data)
    if changed:
        homework.save()
        domain.record("homework.homework.updated", homework, fields=changed)
    return homework


@transaction.atomic
def archive(actor: Actor, homework: Homework) -> None:
    _check_owner(actor, homework)
    if homework.status != HomeworkStatus.ARCHIVED:
        homework.status = HomeworkStatus.ARCHIVED
        homework.save(update_fields=["status", "updated_at"])
        domain.record("homework.homework.archived", homework)


@transaction.atomic
def submit(actor: Actor, homework: Homework, *, text: str = "", upload: Any = None) -> Submission:
    """The signed-in student submits (or replaces a submission that has not been reviewed)."""
    student = Student.objects.filter(school_id=actor.school.pk, membership=actor.membership).first()
    if student is None or not roster(homework).filter(pk=student.pk).exists():
        raise PermissionDenied("Only a student of this class can submit.")
    if homework.status == HomeworkStatus.ARCHIVED:
        raise Conflict("This homework is archived.")
    if not text and upload is None:
        raise ValidationError({"text": ["Write an answer or attach a file."]})
    existing = Submission.objects.select_for_update().filter(homework=homework, student=student).first()
    if existing is not None and existing.status == SubmissionStatus.REVIEWED:
        raise Conflict("This submission has been reviewed; it cannot be replaced.")
    late = clock.today(actor.school) > homework.due_date
    stored = files.store(actor.school.pk, upload, actor.membership) if upload is not None else None
    submission = existing or Submission(school=actor.school, homework=homework, student=student)
    submission.text = text
    submission.status = SubmissionStatus.LATE if late else SubmissionStatus.SUBMITTED
    submission.submitted_at = timezone.now()
    submission.file = stored
    domain.save(submission, conflict="This homework was already submitted.")
    domain.record("homework.submission.submitted", submission, homework=str(homework.pk), late=late or None)
    return submission


@transaction.atomic
def review(
    actor: Actor, submission: Submission, *, feedback: str = "", score: Decimal | None = None
) -> Submission:
    homework = submission.homework
    if not may_write(actor, REVIEW, homework.section, homework.subject):
        raise PermissionDenied("You can review homework only for a section and subject you teach.")
    if score is not None and homework.max_score is not None and score > homework.max_score:
        raise ValidationError({"score": [f"The score is above the maximum ({homework.max_score})."]})
    submission = Submission.objects.select_for_update().get(pk=submission.pk)
    submission.status, submission.feedback, submission.score = SubmissionStatus.REVIEWED, feedback, score
    submission.reviewed_by, submission.reviewed_at = actor.membership, timezone.now()
    submission.save()
    domain.record("homework.submission.reviewed", submission, homework=str(homework.pk))
    notifications.notify(
        actor.school,
        notifications.family_of(submission.student),
        kind="homework",
        title=f"Homework reviewed: {homework.title}",
        body=feedback[:200],
        student=submission.student,
        link=("homework", homework.pk),
    )
    return submission


def status_board(homework: Homework, students: QuerySet[Student]) -> list[dict[str, Any]]:
    """One row per student: their stored submission, or ``pending``."""
    stored = {s.student_id: s for s in homework.submissions.all()}
    rows = []
    for student in students.order_by("first_name", "last_name"):
        sub = stored.get(student.pk)
        rows.append({"student": student, "submission": sub, "status": sub.status if sub else "pending"})
    return rows


def missed_counts(school: Any, since: datetime.date, until: datetime.date) -> dict[Any, int]:
    """Per student: published homework due in ``[since, until]`` never submitted (a monitoring input)."""
    due = Homework.objects.filter(
        school_id=school.pk, status=HomeworkStatus.PUBLISHED, due_date__gte=since, due_date__lte=until
    )
    counts: dict[Any, int] = {}
    for hw in due:
        submitted = set(hw.submissions.values_list("student_id", flat=True))
        enrolled = Enrollment.objects.filter(section_id=hw.section_id, status="active").values_list(
            "student_id", flat=True
        )
        for student_id in enrolled:
            if student_id not in submitted:
                counts[student_id] = counts.get(student_id, 0) + 1
    return counts


def unreviewed_counts(school: Any) -> dict[Any, int]:
    """Per teacher (staff profile): submissions waiting for review (a monitoring input)."""
    counts: dict[Any, int] = {}
    rows = Submission.objects.filter(
        school_id=school.pk,
        status__in=[SubmissionStatus.SUBMITTED, SubmissionStatus.LATE],
        homework__status=HomeworkStatus.PUBLISHED,
    ).values_list("homework__teacher_id", flat=True)
    for teacher_id in rows:
        if teacher_id is not None:
            counts[teacher_id] = counts.get(teacher_id, 0) + 1
    return counts
