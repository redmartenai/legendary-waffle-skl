"""The library (screen documentation "Library": catalogue, issue and return, overdue, fines; role
"Librarian").

* A **book** is a catalogue title; each physical **copy** has its own accession number and state.
* A **loan** lends one copy to a student (signed in or not) or to a staff member until a due date. A
  copy is on at most one open loan (database constraint). Renewal moves the due date.
* **Fines**: no source defines a loan period or a fine rate, so both are the school's settings. With no
  fine rate set, no fine is charged; with no loan period set, every issue must give its due date.
"""

from __future__ import annotations

from django.db import models
from django.db.models import F, Q

from eduflow.core.ids import uuid7
from eduflow.people.models import Student
from eduflow.tenancy.models import Membership, TenantModel, TenantQuerySet


class LibrarySettings(TenantModel):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    loan_days = models.PositiveSmallIntegerField(null=True, blank=True)
    fine_per_day = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    max_loans = models.PositiveSmallIntegerField(null=True, blank=True)

    objects = TenantQuerySet.as_manager()

    class Meta:
        db_table = "library_settings"
        constraints = [
            models.UniqueConstraint(fields=["school"], name="library_settings_school_uniq"),
            models.CheckConstraint(
                condition=Q(fine_per_day__isnull=True) | Q(fine_per_day__gte=0), name="library_fine_check"
            ),
        ]


class Book(TenantModel):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    title = models.CharField(max_length=300)
    authors = models.CharField(max_length=300, blank=True)
    isbn = models.CharField(max_length=17, blank=True)
    publisher = models.CharField(max_length=200, blank=True)
    category = models.CharField(max_length=100, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = TenantQuerySet.as_manager()

    class Meta:
        db_table = "library_book"
        constraints = [models.UniqueConstraint(fields=["id", "school"], name="library_book_id_school_uniq")]


class CopyStatus(models.TextChoices):
    AVAILABLE = "available", "Available"
    ON_LOAN = "on_loan", "On loan"
    LOST = "lost", "Lost"
    WITHDRAWN = "withdrawn", "Withdrawn"


class Copy(TenantModel):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    book = models.ForeignKey(Book, on_delete=models.PROTECT, related_name="copies")
    accession_number = models.CharField(max_length=32)
    shelf = models.CharField(max_length=50, blank=True)
    status = models.CharField(max_length=16, choices=CopyStatus.choices, default=CopyStatus.AVAILABLE)

    objects = TenantQuerySet.as_manager()

    class Meta:
        db_table = "library_copy"
        constraints = [
            models.UniqueConstraint(fields=["school", "accession_number"], name="library_accession_uniq"),
            models.UniqueConstraint(fields=["id", "school"], name="library_copy_id_school_uniq"),
        ]


class Loan(TenantModel):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    copy = models.ForeignKey(Copy, on_delete=models.PROTECT, related_name="loans")
    student = models.ForeignKey(
        Student, on_delete=models.PROTECT, null=True, blank=True, related_name="loans"
    )
    member = models.ForeignKey(
        Membership,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        help_text="A staff borrower.",
    )
    issued_on = models.DateField()
    due_on = models.DateField()
    returned_on = models.DateField(null=True, blank=True)
    renewals = models.PositiveSmallIntegerField(default=0)
    fine = models.DecimalField(max_digits=8, decimal_places=2, default=0)
    fine_paid = models.BooleanField(default=False)
    issued_by = models.ForeignKey(Membership, on_delete=models.SET_NULL, null=True, related_name="+")

    objects = TenantQuerySet.as_manager()

    class Meta:
        db_table = "library_loan"
        constraints = [
            models.UniqueConstraint(
                fields=["copy"], condition=Q(returned_on__isnull=True), name="library_one_open_loan_per_copy"
            ),
            models.CheckConstraint(condition=Q(due_on__gte=F("issued_on")), name="library_loan_due_check"),
            models.CheckConstraint(
                condition=Q(returned_on__isnull=True) | Q(returned_on__gte=F("issued_on")),
                name="library_loan_return_check",
            ),
            models.CheckConstraint(condition=Q(fine__gte=0), name="library_loan_fine_check"),
            models.CheckConstraint(
                condition=Q(student__isnull=True) ^ Q(member__isnull=True),
                name="library_loan_one_borrower_check",
            ),
            models.UniqueConstraint(fields=["id", "school"], name="library_loan_id_school_uniq"),
        ]
