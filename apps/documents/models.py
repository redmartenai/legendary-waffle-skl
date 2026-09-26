from django.conf import settings
from django.db import models

from apps.core.models import SchoolScopedModel


class Folder(SchoolScopedModel):
    """A folder in the school's document repository. Locked folders open for management only."""

    name = models.CharField(max_length=80)
    parent = models.ForeignKey("self", null=True, blank=True, on_delete=models.CASCADE, related_name="children")
    locked = models.BooleanField(default=False)
    position = models.PositiveSmallIntegerField(default=0)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")

    class Meta:
        ordering = ["position", "name"]


class Document(SchoolScopedModel):
    """A file the school shares: a circular, a report card, a certificate, a policy.

    Who can see it is decided by `audience` (+ `class_groups` / `student`); every download is logged.
    """

    class Kind(models.TextChoices):
        CIRCULAR = "circular", "Circular"
        REPORT_CARD = "report_card", "Report card"
        CERTIFICATE = "certificate", "Certificate"
        POLICY = "policy", "Policy"
        LESSON_PLAN = "lesson_plan", "Lesson plan"
        QUESTION_PAPER = "question_paper", "Question paper"
        PAYSLIP = "payslip", "Payslip"
        OTHER = "other", "Other"

    class Audience(models.TextChoices):
        EVERYONE = "everyone", "Everyone"
        FAMILIES = "families", "All families"
        CLASSES = "classes", "Families of some classes"
        STUDENT = "student", "One student's family"
        STAFF = "staff", "Staff"
        # Only the owner (and the principal/admin): payslips, a teacher's own certificates.
        PRIVATE = "private", "Owner only"

    class Status(models.TextChoices):
        NONE = "", "—"
        SUBMITTED = "submitted", "Sent to exam cell"
        ACCEPTED = "accepted", "Accepted"
        IN_REVIEW = "in_review", "In review"
        VERIFIED = "verified", "Verified"

    kind = models.CharField(max_length=16, choices=Kind.choices)
    title = models.CharField(max_length=160)
    subtitle = models.CharField(max_length=160, blank=True)
    file = models.FileField(upload_to="documents/%Y/%m/")
    size = models.PositiveIntegerField(default=0)
    audience = models.CharField(max_length=10, choices=Audience.choices, default=Audience.FAMILIES)
    class_groups = models.ManyToManyField("academics.ClassGroup", blank=True, related_name="+")
    student = models.ForeignKey("academics.Student", null=True, blank=True, on_delete=models.CASCADE, related_name="documents")
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")
    issued_on = models.DateField(null=True, blank=True)
    # Staff documents: lesson plans are shared with the subject's department; question papers go to the exam cell.
    subject = models.ForeignKey("academics.Subject", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.NONE, blank=True)
    # Exam-cell lock: a question paper nobody but the exam cell can open until this date.
    locked_until = models.DateField(null=True, blank=True)
    # The console repository: where it's filed, which version this is, and how many pages it has.
    folder = models.ForeignKey(Folder, null=True, blank=True, on_delete=models.SET_NULL, related_name="documents")
    version = models.PositiveSmallIntegerField(default=1)
    pages = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["-created_at"]


class DocumentGrant(SchoolScopedModel):
    """One row of a document's permissions matrix: who (a role, scoped to classes or a route) may view,
    download or upload a new version. The principal and admins always have every right (fixed by policy)."""

    class Subject(models.TextChoices):
        STAFF = "staff", "All staff"
        CLASS_TEACHER = "class_teacher", "Class teacher"
        TEACHERS = "teachers", "Teachers"
        PARENTS = "parents", "Parents"
        STUDENTS = "students", "Students"
        ACCOUNTANT = "accountant", "Accountant"

    document = models.ForeignKey(Document, on_delete=models.CASCADE, related_name="grants")
    subject = models.CharField(max_length=16, choices=Subject.choices)
    # No classes and no route: the whole school.
    class_groups = models.ManyToManyField("academics.ClassGroup", blank=True, related_name="+")
    route = models.ForeignKey("transport.Route", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    can_view = models.BooleanField(default=False)
    can_download = models.BooleanField(default=False)
    can_upload = models.BooleanField(default=False)
    position = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["position"]


class DocumentDownload(SchoolScopedModel):
    """Audit log: who downloaded which document (or generated PDF) and when.

    The console also logs views and permission changes here (``action``), with the device.
    """

    class Action(models.TextChoices):
        DOWNLOAD = "download", "Downloaded"
        VIEW = "view", "Viewed"
        PERMISSIONS = "permissions", "Changed permissions"
        UPLOAD = "upload", "Uploaded"

    document = models.ForeignKey(Document, null=True, blank=True, on_delete=models.CASCADE, related_name="downloads")
    # Generated documents (receipts, report cards) have no Document row; they are identified by kind + ref.
    kind = models.CharField(max_length=20)
    ref = models.CharField(max_length=64, blank=True)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")
    student = models.ForeignKey("academics.Student", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    action = models.CharField(max_length=12, choices=Action.choices, default=Action.DOWNLOAD)
    device = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["-created_at"]


class CertificateRequest(SchoolScopedModel):
    """A family asking the office for a certificate (bonafide, character, study)."""

    class Kind(models.TextChoices):
        BONAFIDE = "bonafide", "Bonafide certificate"
        CHARACTER = "character", "Character certificate"
        STUDY = "study", "Study certificate"
        TRANSFER = "transfer", "Transfer certificate"

    class Status(models.TextChoices):
        REQUESTED = "requested", "Requested"
        READY = "ready", "Ready"
        DECLINED = "declined", "Declined"

    student = models.ForeignKey("academics.Student", on_delete=models.CASCADE, related_name="certificate_requests")
    kind = models.CharField(max_length=10, choices=Kind.choices)
    purpose = models.CharField(max_length=200, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.REQUESTED)
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")
    document = models.ForeignKey(Document, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

    class Meta:
        ordering = ["-created_at"]
