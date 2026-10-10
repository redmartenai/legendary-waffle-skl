"""Documents: a permission-controlled repository (screen documentation: "view/download/upload actions"), and
the stored-file record every module uses for uploads (homework attachments, learning content, receipts).

* ``StoredFile``: an uploaded file's metadata; the bytes live in the private ``documents`` storage under
  ``documents/<school>/<file id>``. It has no access rules of its own: the record that refers to it (a
  document, a homework attachment, a lesson) decides who may download it.
* ``Document``: a repository entry with an audience list (CURRENT_STATE §8: principal, staff, class_teacher,
  teachers, parents, students, accountant), optionally about one student or one staff member.
"""

from __future__ import annotations

from django.contrib.postgres.fields import ArrayField
from django.db import models
from django.db.models import Q

from eduflow.core.ids import uuid7
from eduflow.people.models import StaffProfile, Student
from eduflow.tenancy.models import Membership, TenantModel, TenantQuerySet


class StoredFile(TenantModel):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    filename = models.CharField(max_length=200)
    content_type = models.CharField(max_length=100)
    size = models.PositiveIntegerField()
    sha256 = models.CharField(max_length=64)
    storage_key = models.CharField(max_length=255, unique=True, help_text="Internal; never returned.")
    uploaded_by = models.ForeignKey(Membership, on_delete=models.SET_NULL, null=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    objects = TenantQuerySet.as_manager()

    class Meta:
        db_table = "documents_stored_file"
        constraints = [
            models.UniqueConstraint(fields=["id", "school"], name="documents_stored_file_id_school_uniq")
        ]


class Audience(models.TextChoices):
    PRINCIPAL = "principal", "Principal"
    STAFF = "staff", "Staff"
    CLASS_TEACHER = "class_teacher", "Class teacher"
    TEACHERS = "teachers", "Teachers"
    PARENTS = "parents", "Parents"
    STUDENTS = "students", "Students"
    ACCOUNTANT = "accountant", "Accountant"


class Document(TenantModel):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    title = models.CharField(max_length=200)
    category = models.CharField(
        max_length=60, blank=True, help_text="Free text, e.g. Certificates, Circulars."
    )
    file = models.ForeignKey(StoredFile, on_delete=models.PROTECT, related_name="+")
    student = models.ForeignKey(
        Student, on_delete=models.PROTECT, null=True, blank=True, related_name="documents"
    )
    staff = models.ForeignKey(
        StaffProfile, on_delete=models.PROTECT, null=True, blank=True, related_name="documents"
    )
    audiences = ArrayField(models.CharField(max_length=16, choices=Audience.choices), default=list)
    uploaded_by = models.ForeignKey(Membership, on_delete=models.SET_NULL, null=True, related_name="+")
    archived_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = TenantQuerySet.as_manager()

    class Meta:
        db_table = "documents_document"
        constraints = [
            models.CheckConstraint(
                condition=Q(student__isnull=True) | Q(staff__isnull=True),
                name="documents_document_one_owner_check",
            ),
            models.CheckConstraint(
                condition=Q(audiences__contained_by=Audience.values),
                name="documents_document_audiences_check",
            ),
            models.UniqueConstraint(fields=["id", "school"], name="documents_document_id_school_uniq"),
        ]
        indexes = [models.Index(fields=["school", "student"], name="documents_document_student_idx")]
