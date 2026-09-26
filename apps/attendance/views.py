from datetime import date, timedelta

from django.http import Http404
from rest_framework import serializers, status
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.academics.access import can_manage_class, student_for_request
from apps.academics.models import ClassGroup, Student
from apps.accounts.models import MANAGEMENT_ROLES, Role
from apps.core.api import SchoolAPIView
from apps.core.utils import school_today
from apps.notifications.models import Category
from apps.notifications.services import notify

from .models import AttendanceException, AttendanceSession, AttendanceStatus, LeaveApplication
from .services import mark_class_attendance, student_month, student_year


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


def _past_cutoff(request, day: date) -> bool:
    from apps.core.utils import parse_hhmm, school_now

    now = school_now(request.school)
    cutoff = parse_hhmm(request.school.policy("attendance", "edit_cutoff") or "10:00")
    return day < now.date() or (day == now.date() and now.time() >= cutoff)


def _locked(request, group: ClassGroup, day: date, entries: list[dict]) -> bool:
    """A marked register can't be changed after the cutoff (a retry with the same marks is fine)."""
    session = AttendanceSession.objects.filter(class_group=group, date=day).first()
    if session is None or not _past_cutoff(request, day):
        return False
    current = {str(e.student_id): e.status for e in AttendanceException.objects.filter(session=session)}
    wanted = {e["student_id"]: e["status"] for e in entries if e["status"] != "present"}
    return current != wanted


def _request_correction(request, group: ClassGroup, day: date, entries: list[dict]):
    from apps.approvals.services import open_request

    from .models import AttendanceCorrection

    session = AttendanceSession.objects.get(class_group=group, date=day)
    current = {str(e.student_id): e.status for e in AttendanceException.objects.filter(session=session)}
    wanted = {e["student_id"]: e["status"] for e in entries if e["status"] != "present"}
    changes = [
        {"student_id": sid, "from": current.get(sid, "present"), "to": wanted.get(sid, "present")}
        for sid in sorted(set(current) | set(wanted))
        if current.get(sid, "present") != wanted.get(sid, "present")
    ]
    corr = AttendanceCorrection.objects.create(session=session, entries=changes, reason=str(request.data.get("reason", ""))[:300], requested_by=request.user)
    open_request(
        kind="attendance",
        target=corr,
        requested_by=request.user,
        summary=f"{group.short_label} register · {day:%a %d %b} · {len(changes)} change{'s' if len(changes) != 1 else ''}",
        due_on=day,
    )
    return corr


class ClassRosterView(SchoolAPIView):
    """Students in a class with their status for a day (defaults to today)."""

    allowed_roles = frozenset({Role.TEACHER}) | MANAGEMENT_ROLES
    permission = ("attendance", "view")

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
                "cutoff": request.school.policy("attendance", "edit_cutoff"),
                # Once marked, changes after the cutoff go to the principal (management can always edit).
                "locked": bool(session) and not (request.roles & MANAGEMENT_ROLES) and _past_cutoff(request, day),
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
    permission = ("attendance", "create")

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
        entries = [{"student_id": str(e["student_id"]), "status": e["status"]} for e in data.validated_data["entries"]]
        if not (request.roles & MANAGEMENT_ROLES) and _locked(request, group, day, entries):
            # Past the cutoff: the change goes to the principal instead of straight into the register.
            corr = _request_correction(request, group, day, entries)
            return Response({"pending_approval": True, "changes": len(corr.entries)}, status=status.HTTP_202_ACCEPTED)
        summary = mark_class_attendance(
            group,
            day,
            entries,
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
        payload = student_month(student, year, month, today)
        payload["year"] = student_year(student)
        return Response(payload)


class LeaveSerializer(serializers.Serializer):
    from_date = serializers.DateField()
    to_date = serializers.DateField()
    kind = serializers.ChoiceField(choices=LeaveApplication.Kind.choices, default=LeaveApplication.Kind.SICK)
    reason = serializers.CharField(max_length=300)
    half_day = serializers.BooleanField(default=False)

    def validate(self, attrs):
        if attrs["to_date"] < attrs["from_date"]:
            raise ValidationError({"to_date": "The last day can't be before the first."})
        return attrs


def leave_payload(leave: LeaveApplication) -> dict:
    return {
        "id": str(leave.id),
        "from_date": leave.from_date.isoformat(),
        "to_date": leave.to_date.isoformat(),
        "kind": leave.kind,
        "reason": leave.reason,
        "half_day": leave.half_day,
        "status": leave.status,
        "decided_by": leave.decided_by.full_name if leave.decided_by else None,
        "decided_at": leave.decided_at.isoformat() if leave.decided_at else None,
    }


class StudentLeaveView(SchoolAPIView):
    """A child's leave applications; families apply here and the class teacher decides."""

    def get(self, request, student_id):
        student = student_for_request(request, student_id)
        leaves = LeaveApplication.objects.filter(student=student).select_related("decided_by")[:20]
        return Response({"items": [leave_payload(lv) for lv in leaves]})

    def post(self, request, student_id):
        if not request.roles & {Role.PARENT, Role.STUDENT}:
            raise PermissionDenied("Only the family can apply for leave.")
        student = student_for_request(request, student_id)
        data = LeaveSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        if data.validated_data["from_date"] < school_today(request.school) - timedelta(days=7):
            raise ValidationError({"from_date": "Leave can be applied up to a week after the absence."})
        leave = LeaveApplication.objects.create(student=student, applied_by=request.user, **data.validated_data)
        teacher = student.class_group.class_teacher
        if teacher:
            notify(
                [teacher],
                school=request.school,
                category=Category.ATTENDANCE,
                title=f"Leave request for {student.full_name}",
                body=f"{leave.from_date:%a %d %b}" + (f" – {leave.to_date:%a %d %b}" if leave.to_date != leave.from_date else "") + f" · {leave.reason[:120]}",
                data={"student_id": str(student.id), "leave_id": str(leave.id), "type": "leave"},
                dedupe_key=f"leave:{leave.id}",
            )
        return Response(leave_payload(leave), status=status.HTTP_201_CREATED)
