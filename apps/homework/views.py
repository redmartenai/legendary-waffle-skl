from datetime import date, timedelta

from django.http import Http404
from rest_framework import serializers, status
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response

from apps.academics.access import can_manage_class, student_for_request
from apps.academics.models import ClassGroup, Student, Subject, TeachingAssignment
from apps.accounts.models import MANAGEMENT_ROLES, Role
from apps.core.api import SchoolAPIView
from apps.core.utils import school_today

from . import services
from .models import Homework, HomeworkAttachment, HomeworkSignoff, HomeworkSubmission


def _photo_urls(request, submission) -> list[str]:
    return [request.build_absolute_uri(p.image.url) for p in submission.photos.all()]


def homework_payload(
    request,
    homework: Homework,
    submission: HomeworkSubmission | None = None,
    *,
    signoff=None,
    flagged_by: str | None = None,
    due_period: dict | None = None,
) -> dict:
    today = school_today(request.school)
    return {
        "id": str(homework.id),
        "title": homework.title,
        "description": homework.description,
        "subject": {
            "id": str(homework.subject_id),
            "name": homework.subject.name,
            "color": homework.subject.color,
            "code": homework.subject.code,
            "short": homework.subject.short_name,
        },
        "class": {"id": str(homework.class_group_id), "label": homework.class_group.label},
        "assigned_on": homework.assigned_on.isoformat(),
        "due_date": homework.due_date.isoformat(),
        "overdue": homework.due_date < today and submission is None,
        "accepts_photos": homework.accepts_photos,
        "assigned_by": homework.assigned_by.full_name if homework.assigned_by else None,
        "assigned_by_id": str(homework.assigned_by_id) if homework.assigned_by_id else None,
        "attachments": [
            {"id": str(a.id), "name": a.name, "size": a.size, "url": request.build_absolute_uri(a.file.url)}
            for a in homework.attachments.all()
        ],
        "signed": {"at": signoff.created_at.isoformat(), "by": signoff.user.full_name} if signoff else None,
        "flagged_by": flagged_by,
        "due_period": due_period,
        "submission": {
            "id": str(submission.id),
            "status": submission.status,
            "submitted_at": submission.submitted_at.isoformat(),
            "teacher_remark": submission.teacher_remark,
            "grade": submission.grade,
            "in_notebook": submission.in_notebook,
            "checked_copy": request.build_absolute_uri(submission.checked_copy.url) if submission.checked_copy else None,
            "photos": _photo_urls(request, submission),
        }
        if submission
        else None,
    }


class StudentHomeworkView(SchoolAPIView):
    """A child's homework. `?week=YYYY-MM-DD` (any day of the week) limits it to work due that week."""

    def get(self, request, student_id):
        from apps.academics.models import Remark, TimetableSlot

        student = student_for_request(request, student_id)
        items = Homework.objects.filter(class_group=student.class_group).select_related(
            "subject", "class_group", "assigned_by"
        ).prefetch_related("attachments")
        week = request.query_params.get("week")
        if week:
            try:
                day = date.fromisoformat(week)
            except ValueError as exc:
                raise ValidationError({"week": "Use YYYY-MM-DD."}) from exc
            monday = day - timedelta(days=day.weekday())
            items = items.filter(due_date__range=(monday, monday + timedelta(days=6)))
        items = list(items[:60])
        submissions = {
            s.homework_id: s
            for s in HomeworkSubmission.objects.filter(student=student, homework__in=items).prefetch_related("photos")
        }
        signoffs = {
            s.homework_id: s for s in HomeworkSignoff.objects.filter(student=student, homework__in=items).select_related("user")
        }
        flags = {
            r.homework_id: r.author.full_name
            for r in Remark.objects.filter(
                student=student, homework__in=items, tone="concern", visibility=Remark.Visibility.FAMILY
            ).select_related("author")
        }
        # "Due tomorrow · P1": the first period of that subject on the due date.
        slots: dict = {}
        for slot in TimetableSlot.objects.filter(class_group=student.class_group).order_by("period"):
            slots.setdefault((slot.weekday, slot.subject_id), {"period": slot.period, "starts_at": slot.starts_at.strftime("%H:%M")})
        payload = [
            homework_payload(
                request,
                h,
                submissions.get(h.id),
                signoff=signoffs.get(h.id),
                flagged_by=flags.get(h.id),
                due_period=slots.get((h.due_date.weekday(), h.subject_id)),
            )
            for h in items
        ]
        today = school_today(request.school)
        pending = sum(1 for h in payload if h["submission"] is None and h["due_date"] >= today.isoformat())
        return Response({"pending": pending, "items": payload})


class HomeworkSubmitView(SchoolAPIView):
    parser_classes = [MultiPartParser, FormParser, JSONParser]
    allowed_roles = frozenset({Role.PARENT, Role.STUDENT})

    def post(self, request, homework_id):
        homework = Homework.objects.filter(id=homework_id).select_related("subject", "class_group").first()
        if homework is None:
            raise Http404
        student = student_for_request(request, request.data.get("student_id"))
        if student.class_group_id != homework.class_group_id:
            raise Http404
        submission = services.submit(
            homework, student, request.user, request.FILES.getlist("photos"), str(request.data.get("client_id", ""))
        )
        submission = HomeworkSubmission.objects.prefetch_related("photos").get(pk=submission.pk)
        return Response(homework_payload(request, homework, submission), status=status.HTTP_201_CREATED)


class HomeworkDoneView(SchoolAPIView):
    """Marks homework as done in the notebook (nothing to upload)."""

    allowed_roles = frozenset({Role.STUDENT, Role.PARENT})

    def post(self, request, homework_id):
        homework = Homework.objects.filter(id=homework_id).select_related("subject", "class_group").first()
        if homework is None:
            raise Http404
        student = student_for_request(request, request.data.get("student_id"))
        if student.class_group_id != homework.class_group_id:
            raise Http404
        submission = services.mark_done_in_notebook(homework, student, request.user)
        return Response(homework_payload(request, homework, submission), status=status.HTTP_201_CREATED)


class HomeworkSignoffView(SchoolAPIView):
    """A parent signs the diary to say they've seen a piece of homework."""

    allowed_roles = frozenset({Role.PARENT})

    def post(self, request, homework_id):
        homework = Homework.objects.filter(id=homework_id).first()
        if homework is None:
            raise Http404
        student = student_for_request(request, request.data.get("student_id"))
        if student.class_group_id != homework.class_group_id:
            raise Http404
        signoff, _ = HomeworkSignoff.objects.get_or_create(homework=homework, student=student, defaults={"user": request.user})
        return Response({"at": signoff.created_at.isoformat(), "by": signoff.user.full_name})


class CreateHomeworkSerializer(serializers.Serializer):
    subject_id = serializers.UUIDField()
    title = serializers.CharField(max_length=120)
    description = serializers.CharField(max_length=2000, required=False, allow_blank=True)
    due_date = serializers.DateField()
    accepts_photos = serializers.BooleanField(default=True)
    notify_parents = serializers.BooleanField(default=True)


ATTACHMENT_TYPES = ("application/pdf", "image/", "application/msword", "application/vnd.openxmlformats-officedocument")
MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024


class ClassHomeworkView(SchoolAPIView):
    allowed_roles = frozenset({Role.TEACHER}) | MANAGEMENT_ROLES
    permission = {"POST": ("homework", "create")}
    parser_classes = [JSONParser, MultiPartParser, FormParser]

    def _group(self, request, class_id) -> ClassGroup:
        group = ClassGroup.objects.filter(id=class_id).first()
        if group is None or not can_manage_class(request, group):
            raise Http404
        return group

    def get(self, request, class_id):
        group = self._group(request, class_id)
        items = Homework.objects.filter(class_group=group).select_related("subject", "class_group", "assigned_by")[:50]
        class_size = Student.objects.filter(class_group=group, is_active=True).count()
        counts = {}
        for submission in HomeworkSubmission.objects.filter(homework__in=items).values("homework_id", "status"):
            bucket = counts.setdefault(submission["homework_id"], {"submitted": 0, "reviewed": 0})
            bucket["submitted"] += 1
            if submission["status"] != HomeworkSubmission.Status.SUBMITTED:
                bucket["reviewed"] += 1
        return Response(
            {
                "class_size": class_size,
                "items": [
                    {**homework_payload(request, h), "counts": counts.get(h.id, {"submitted": 0, "reviewed": 0})}
                    for h in items
                ],
            }
        )

    def post(self, request, class_id):
        group = self._group(request, class_id)
        data = CreateHomeworkSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        subject = Subject.objects.filter(id=data.validated_data["subject_id"]).first()
        if subject is None:
            raise ValidationError({"subject_id": "Unknown subject."})
        teaches_it = TeachingAssignment.objects.filter(teacher=request.user, class_group=group, subject=subject).exists()
        if not teaches_it and not (request.roles & MANAGEMENT_ROLES) and group.class_teacher_id != request.user.id:
            raise PermissionDenied("You don't teach this subject in this class.")
        today = school_today(request.school)
        if data.validated_data["due_date"] < today:
            raise ValidationError({"due_date": "The due date has passed."})
        files = request.FILES.getlist("attachments")
        for f in files:
            if f.size > MAX_ATTACHMENT_BYTES:
                raise ValidationError({"attachments": "Each file can be up to 10 MB."})
            if not (f.content_type or "").startswith(ATTACHMENT_TYPES):
                raise ValidationError({"attachments": "Attach a PDF, Word file or photo."})
        homework = Homework.objects.create(
            class_group=group,
            subject=subject,
            title=data.validated_data["title"],
            description=data.validated_data.get("description", ""),
            assigned_by=request.user,
            assigned_on=today,
            due_date=data.validated_data["due_date"],
            accepts_photos=data.validated_data["accepts_photos"],
        )
        for f in files:
            HomeworkAttachment.objects.create(homework=homework, file=f, name=f.name[:120], size=f.size)
        services.announce_new(homework, parents=data.validated_data["notify_parents"])
        return Response(homework_payload(request, homework), status=status.HTTP_201_CREATED)


class HomeworkSubmissionsView(SchoolAPIView):
    allowed_roles = frozenset({Role.TEACHER}) | MANAGEMENT_ROLES

    def get(self, request, homework_id):
        homework = Homework.objects.filter(id=homework_id).select_related("subject", "class_group").first()
        if homework is None or not can_manage_class(request, homework.class_group):
            raise Http404
        submissions = {
            s.student_id: s
            for s in HomeworkSubmission.objects.filter(homework=homework).prefetch_related("photos")
        }
        students = Student.objects.filter(class_group=homework.class_group, is_active=True).order_by("roll_no")
        return Response(
            {
                "homework": homework_payload(request, homework),
                "students": [
                    {
                        "id": str(s.id),
                        "name": s.full_name,
                        "initials": s.initials,
                        "submission": {
                            "id": str(submissions[s.id].id),
                            "status": submissions[s.id].status,
                            "submitted_at": submissions[s.id].submitted_at.isoformat(),
                            "teacher_remark": submissions[s.id].teacher_remark,
                            "photos": _photo_urls(request, submissions[s.id]),
                        }
                        if s.id in submissions
                        else None,
                    }
                    for s in students
                ],
            }
        )


class SubmissionReviewView(SchoolAPIView):
    allowed_roles = frozenset({Role.TEACHER}) | MANAGEMENT_ROLES

    def post(self, request, submission_id):
        submission = (
            HomeworkSubmission.objects.filter(id=submission_id)
            .select_related("homework", "homework__class_group", "homework__subject", "student")
            .first()
        )
        if submission is None or not can_manage_class(request, submission.homework.class_group):
            raise Http404
        services.review(submission, request.data.get("status"), request.data.get("remark", ""))
        return Response({"id": str(submission.id), "status": submission.status, "teacher_remark": submission.teacher_remark})


class TeacherHomeworkView(SchoolAPIView):
    """Recent homework the teacher set (or that is set in their subjects), with hand-in and review counts."""

    allowed_roles = frozenset({Role.TEACHER}) | MANAGEMENT_ROLES

    def get(self, request):
        today = school_today(request.school)
        pairs = set(TeachingAssignment.objects.filter(teacher=request.user).values_list("class_group_id", "subject_id"))
        since = today - timedelta(days=14)
        items = [
            h
            for h in Homework.objects.filter(class_group_id__in={c for c, _ in pairs}, due_date__gte=since)
            .select_related("subject", "class_group", "assigned_by")
            .prefetch_related("attachments")
            .order_by("-assigned_on", "-created_at")[:30]
            if (h.class_group_id, h.subject_id) in pairs or h.assigned_by_id == request.user.id
        ]
        sizes = {}
        counts: dict = {}
        for row in HomeworkSubmission.objects.filter(homework__in=items).values("homework_id", "status"):
            bucket = counts.setdefault(row["homework_id"], {"submitted": 0, "to_review": 0})
            bucket["submitted"] += 1
            if row["status"] == HomeworkSubmission.Status.SUBMITTED:
                bucket["to_review"] += 1
        payload = []
        for h in items:
            if h.class_group_id not in sizes:
                sizes[h.class_group_id] = Student.objects.filter(class_group_id=h.class_group_id, is_active=True).count()
            payload.append(
                {
                    **homework_payload(request, h),
                    "class": {"id": str(h.class_group_id), "label": h.class_group.label, "short_label": h.class_group.short_label},
                    "class_size": sizes[h.class_group_id],
                    "open": h.due_date >= today,
                    "counts": counts.get(h.id, {"submitted": 0, "to_review": 0}),
                }
            )
        return Response({"items": payload})
