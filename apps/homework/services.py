import io
import uuid

from django.core.files.base import ContentFile
from django.db import transaction
from django.utils import timezone
from PIL import Image, ImageOps, UnidentifiedImageError
from rest_framework.exceptions import ValidationError

from apps.academics.access import guardians_of
from apps.academics.models import Student
from apps.notifications.models import Category
from apps.notifications.services import notify

from .models import Homework, HomeworkSubmission, SubmissionPhoto

MAX_PHOTOS = 10
MAX_PHOTO_BYTES = 8 * 1024 * 1024
MAX_EDGE_PX = 2000  # keeps faint pencil legible while staying light on mobile data


def clean_photo(upload) -> ContentFile:
    """Re-encode as JPEG: fixes rotation, bounds the size and drops EXIF (including GPS location)."""
    if upload.size > MAX_PHOTO_BYTES:
        raise ValidationError({"photos": "Each photo must be under 8 MB."})
    try:
        image = Image.open(upload)
        image = ImageOps.exif_transpose(image)
    except (UnidentifiedImageError, OSError) as exc:
        raise ValidationError({"photos": "Upload JPEG or PNG photos."}) from exc
    image = image.convert("RGB")
    image.thumbnail((MAX_EDGE_PX, MAX_EDGE_PX))
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=82, optimize=True)  # no exif= argument: metadata is dropped
    return ContentFile(buffer.getvalue(), name=f"{uuid.uuid4().hex}.jpg")


def submit(homework: Homework, student: Student, user, photos: list, client_id: str = "") -> HomeworkSubmission:
    if not homework.accepts_photos:
        raise ValidationError({"photos": "This homework doesn't take photo submissions."})
    if not photos:
        raise ValidationError({"photos": "Add at least one photo."})
    if len(photos) > MAX_PHOTOS:
        raise ValidationError({"photos": f"Up to {MAX_PHOTOS} photos."})
    cleaned = [clean_photo(p) for p in photos]

    with transaction.atomic():
        submission, created = HomeworkSubmission.objects.get_or_create(
            homework=homework,
            student=student,
            defaults={"submitted_by": user, "submitted_at": timezone.now(), "client_id": client_id},
        )
        if not created:
            if client_id and submission.client_id == client_id:
                return submission  # retried upload
            submission.photos.all().delete()
            submission.submitted_by = user
            submission.submitted_at = timezone.now()
            submission.status = HomeworkSubmission.Status.SUBMITTED
            submission.teacher_remark = ""
            submission.client_id = client_id
            submission.save()
        for index, content in enumerate(cleaned):
            photo = SubmissionPhoto(submission=submission, order=index)
            photo.image.save(content.name, content, save=False)
            photo.save()
    return submission


def review(submission: HomeworkSubmission, status: str, remark: str) -> HomeworkSubmission:
    if status not in {HomeworkSubmission.Status.REVIEWED, HomeworkSubmission.Status.REDO}:
        raise ValidationError({"status": "Use reviewed or redo."})
    submission.status = status
    submission.teacher_remark = (remark or "")[:500]
    submission.save(update_fields=["status", "teacher_remark", "updated_at"])
    student = submission.student
    homework = submission.homework
    recipients = list(guardians_of([student]).keys())
    if student.user:
        recipients.append(student.user)
    title = "Homework checked" if status == HomeworkSubmission.Status.REVIEWED else "Homework needs another try"
    notify(
        recipients,
        school=homework.school,
        category=Category.HOMEWORK,
        title=f"{title}: {homework.title}",
        body=submission.teacher_remark or f"{homework.subject.name} · {student.first_name}",
        data={"homework_id": str(homework.id), "student_id": str(student.id), "type": "homework_reviewed"},
        dedupe_key=f"homework:{submission.id}:{status}:{submission.updated_at.timestamp():.0f}",
    )
    return submission


def announce_new(homework: Homework) -> None:
    students = list(Student.objects.filter(class_group=homework.class_group, is_active=True))
    recipients = set(guardians_of(students).keys())
    recipients.update(s.user for s in students if s.user)
    notify(
        recipients,
        school=homework.school,
        category=Category.HOMEWORK,
        title=f"New homework: {homework.title}",
        body=f"{homework.subject.name} · due {homework.due_date.strftime('%a %d %b')}",
        data={"homework_id": str(homework.id), "type": "homework_new"},
        dedupe_key=f"homework:{homework.id}:new",
    )
