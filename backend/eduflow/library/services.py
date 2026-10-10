"""Library writes. Transactional and audited (``library.*``)."""

from __future__ import annotations

import datetime
from decimal import Decimal
from typing import Any

from django.db import transaction
from rest_framework.exceptions import ValidationError

from eduflow.authz.grants import Actor
from eduflow.core.api import Conflict
from eduflow.notifications import services as notifications
from eduflow.people.models import StaffProfile, Student
from eduflow.tenancy import clock, domain

from .models import Book, Copy, CopyStatus, LibrarySettings, Loan


def settings_for(school: Any) -> LibrarySettings:
    return LibrarySettings.objects.filter(school_id=school.pk).first() or LibrarySettings(school_id=school.pk)


@transaction.atomic
def set_settings(actor: Actor, **data: Any) -> LibrarySettings:
    row, _ = LibrarySettings.objects.update_or_create(school_id=actor.school.pk, defaults=data)
    domain.record("library.settings.updated", row, fields=sorted(data))
    return row


@transaction.atomic
def add_book(actor: Actor, *, copies: list[dict[str, Any]] | None = None, **data: Any) -> Book:
    book = Book.objects.create(school=actor.school, **data)
    for c in copies or []:
        _add_copy(actor, book, **c)
    domain.record("library.book.created", book, title=book.title, copies=len(copies or []))
    return book


def _add_copy(actor: Actor, book: Book, *, accession_number: str, shelf: str = "") -> Copy:
    copy = Copy(school=actor.school, book=book, accession_number=accession_number, shelf=shelf)
    return domain.save(copy, conflict=f"Accession number {accession_number} is already used.")


@transaction.atomic
def add_copy(actor: Actor, book: Book, **data: Any) -> Copy:
    copy = _add_copy(actor, book, **data)
    domain.record("library.copy.added", copy, book=str(book.pk))
    return copy


@transaction.atomic
def set_copy_status(actor: Actor, copy: Copy, status: str) -> Copy:
    copy = Copy.objects.select_for_update().get(pk=copy.pk)
    if copy.status == CopyStatus.ON_LOAN or status == CopyStatus.ON_LOAN:
        raise Conflict("Use issue and return to lend a copy.")
    copy.status = status
    copy.save(update_fields=["status"])
    domain.record("library.copy.status_changed", copy, status=status)
    return copy


def _borrower(actor: Actor, student_id: Any, staff_id: Any) -> dict[str, Any]:
    if bool(student_id) == bool(staff_id):
        raise ValidationError({"student_id": ["Give either a student or a staff member."]})
    if student_id:
        return {"student": domain.resolve(Student, actor.school, student_id, "student_id", label="student")}
    staff = domain.resolve(StaffProfile, actor.school, staff_id, "staff_id", label="staff member")
    return {"member": staff.membership}


@transaction.atomic
def issue(
    actor: Actor,
    *,
    copy_id: Any,
    student_id: Any = None,
    staff_id: Any = None,
    due_on: datetime.date | None = None,
) -> Loan:
    borrower = _borrower(actor, student_id, staff_id)
    copy = Copy.objects.select_for_update().filter(school_id=actor.school.pk, pk=copy_id).first()
    if copy is None:
        raise ValidationError({"copy_id": ["Unknown copy."]})
    if copy.status != CopyStatus.AVAILABLE:
        raise Conflict(f"This copy is {copy.get_status_display().lower()}.")
    conf = settings_for(actor.school)
    today = clock.today(actor.school)
    if due_on is None:
        if not conf.loan_days:
            raise ValidationError({"due_on": ["Give a due date (the school has not set a loan period)."]})
        due_on = today + datetime.timedelta(days=conf.loan_days)
    if due_on < today:
        raise ValidationError({"due_on": ["The due date is in the past."]})
    if conf.max_loans:
        open_loans = Loan.objects.filter(
            school_id=actor.school.pk, returned_on__isnull=True, **borrower
        ).count()
        if open_loans >= conf.max_loans:
            raise Conflict(f"This borrower already has {open_loans} book(s); the limit is {conf.max_loans}.")
    loan = Loan.objects.create(
        school=actor.school, copy=copy, issued_on=today, due_on=due_on, issued_by=actor.membership, **borrower
    )
    copy.status = CopyStatus.ON_LOAN
    copy.save(update_fields=["status"])
    domain.record("library.loan.issued", loan, copy=str(copy.pk), due_on=due_on.isoformat())
    return loan


def fine_for(loan: Loan, on: datetime.date) -> Decimal:
    rate = settings_for(loan.school).fine_per_day
    late_days = (on - loan.due_on).days
    if not rate or late_days <= 0:
        return Decimal(0)
    return rate * late_days


@transaction.atomic
def return_loan(actor: Actor, loan: Loan, *, lost: bool = False) -> Loan:
    loan = Loan.objects.select_for_update(of=("self",)).select_related("copy").get(pk=loan.pk)
    if loan.returned_on is not None:
        raise Conflict("This loan is already closed.")
    today = clock.today(actor.school)
    loan.returned_on, loan.fine = today, fine_for(loan, today)
    loan.save()
    loan.copy.status = CopyStatus.LOST if lost else CopyStatus.AVAILABLE
    loan.copy.save(update_fields=["status"])
    domain.record("library.loan.returned", loan, fine=str(loan.fine), lost=lost or None)
    return loan


@transaction.atomic
def renew(actor: Actor, loan: Loan, *, due_on: datetime.date | None = None) -> Loan:
    loan = Loan.objects.select_for_update().get(pk=loan.pk)
    if loan.returned_on is not None:
        raise Conflict("This loan is closed.")
    conf = settings_for(actor.school)
    if due_on is None:
        if not conf.loan_days:
            raise ValidationError({"due_on": ["Give a new due date (the school has not set a loan period)."]})
        due_on = clock.today(actor.school) + datetime.timedelta(days=conf.loan_days)
    if due_on <= loan.due_on:
        raise ValidationError({"due_on": ["The new due date must be later than the current one."]})
    loan.due_on, loan.renewals = due_on, loan.renewals + 1
    loan.save(update_fields=["due_on", "renewals"])
    domain.record("library.loan.renewed", loan, due_on=due_on.isoformat())
    return loan


@transaction.atomic
def mark_fine_paid(actor: Actor, loan: Loan) -> Loan:
    if loan.returned_on is None or loan.fine <= 0 or loan.fine_paid:
        raise Conflict("There is no unpaid fine on this loan.")
    loan.fine_paid = True
    loan.save(update_fields=["fine_paid"])
    domain.record("library.loan.fine_paid", loan, fine=str(loan.fine))
    return loan


def send_overdue_reminders(school: Any) -> int:
    """Notify borrowers (and a student's family) of overdue books. Run daily by the scheduler."""
    today = clock.today(school)
    sent = 0
    for loan in Loan.objects.filter(
        school_id=school.pk, returned_on__isnull=True, due_on__lt=today
    ).select_related("copy__book", "student", "member"):
        recipients = (
            notifications.family_of(loan.student) if loan.student else [m for m in [loan.member] if m]
        )
        notifications.notify(
            school,
            recipients,
            kind="learning",
            title=f"Library book overdue: {loan.copy.book.title}",
            body=f"Due on {loan.due_on.isoformat()}",
            student=loan.student,
            link=("loan", loan.pk),
        )
        sent += 1
    return sent
