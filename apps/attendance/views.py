from datetime import date

from django.http import Http404
from rest_framework import serializers
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.academics.access import can_manage_class, student_for_request
from apps.academics.models import ClassGroup, Student
from apps.accounts.models import MANAGEMENT_ROLES, Role
from apps.core.api import SchoolAPIView
from apps.core.utils import school_today

from .models import AttendanceException, AttendanceSession, AttendanceStatus
from .services import mark_class_attendance, student_month


class EntrySerializer(serializers.Serializer):
    student_id = serializers.UUIDField()
    status = serializers.ChoiceField(choices=AttendanceStatus.choices)


class MarkSerializer(serializers.Serializer):
    date = serializers.DateField(required=False)
    entries = EntrySerializer(many=True)
    client_id = serializers.CharField(max_length=64, required=False, allow_blank=True)


def _class_or_404(class_id) -> ClassGroup:
    group = ClassGroup.objects.filter(id=class_id).select_related("class_teacher").first()
    if group is None:
        raise Http404
    return group


class ClassRosterView(SchoolAPIView):
    """Students in a class with their status for a day (defaults to today)."""

    allowed_roles = frozenset({Role.TEACHER}) | MANAGEMENT_ROLES

    def get(self, request, class_id):
        group = _class_or_404(class_id)
        if not can_manage_class(request, group):
            raise Http404
        raw = request.query_params.get("date")
        try:
            day = date.fromisoformat(raw) if raw else school_today(request.school)
        except ValueError as exc:
            raise ValidationError({"date": "Use YYYY-MM-DD."}) from exc
        session = AttendanceSession.objects.filter(class_group=group, date=day).first()
        statuses = (
            {str(e.student_id): e.status for e in AttendanceException.objects.filter(session=session)}
            if session
            else {}
        )
        students = Student.objects.filter(class_group=group, is_active=True).order_by("roll_no", "full_name")
        return Response(
            {
                "class": {"id": str(group.id), "label": group.label, "short_label": group.short_label},
                "date": day.isoformat(),
                "marked": session is not None,
                "marked_at": session.marked_at.isoformat() if session else None,
                "students": [
                    {
                        "id": str(s.id),
                        "name": s.full_name,
                        "initials": s.initials,
                        "roll_no": s.roll_no,
                        "status": statuses.get(str(s.id), "present" if session else None),
                    }
                    for s in students
                ],
            }
        )


class ClassAttendanceView(SchoolAPIView):
    allowed_roles = frozenset({Role.TEACHER}) | MANAGEMENT_ROLES

    def post(self, request, class_id):
        group = _class_or_404(class_id)
        if not can_manage_class(request, group):
            raise PermissionDenied("You can only mark attendance for your own classes.")
        data = MarkSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        today = school_today(request.school)
        day = data.validated_data.get("date") or today
        if day > today:
            raise ValidationError({"date": "You can't mark attendance for a future day."})
        if (today - day).days > 7 and not (request.roles & MANAGEMENT_ROLES):
            raise ValidationError({"date": "Older corrections need the principal's approval."})
        summary = mark_class_attendance(
            group,
            day,
            [{"student_id": str(e["student_id"]), "status": e["status"]} for e in data.validated_data["entries"]],
            request.user,
            data.validated_data.get("client_id", ""),
        )
        return Response(summary)


class StudentAttendanceView(SchoolAPIView):
    def get(self, request, student_id):
        student = student_for_request(request, student_id)
        today = school_today(request.school)
        raw = request.query_params.get("month")
        try:
            year, month = (int(p) for p in raw.split("-")) if raw else (today.year, today.month)
            date(year, month, 1)
        except (ValueError, TypeError) as exc:
            raise ValidationError({"month": "Use YYYY-MM."}) from exc
        return Response(student_month(student, year, month, today))
