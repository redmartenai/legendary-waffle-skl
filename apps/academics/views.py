from decimal import Decimal

from django.http import Http404
from rest_framework import serializers, status
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response

from apps.accounts.models import MANAGEMENT_ROLES, Role
from apps.core.api import SchoolAPIView
from apps.core.utils import school_today
from apps.notifications.models import Category
from apps.notifications.services import notify

from .access import children_of, guardians_of, student_for_request, teacher_class_ids
from .models import ClassGroup, Remark, Student, TeachingAssignment, TimetableSlot

WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def student_card(student: Student) -> dict:
    return {
        "id": str(student.id),
        "name": student.full_name,
        "first_name": student.first_name,
        "initials": student.initials,
        "admission_no": student.admission_no,
        "roll_no": student.roll_no,
        "class": {
            "id": str(student.class_group_id),
            "label": student.class_group.label,
            "short_label": student.class_group.short_label,
        },
        "photo_url": student.photo_url or None,
    }


class ParentChildrenView(SchoolAPIView):
    allowed_roles = frozenset({Role.PARENT})

    def get(self, request):
        return Response({"children": [student_card(s) for s in children_of(request.user)]})


class StudentSelfView(SchoolAPIView):
    allowed_roles = frozenset({Role.STUDENT})

    def get(self, request):
        student = Student.objects.filter(user=request.user, is_active=True).select_related("class_group").first()
        if student is None:
            raise Http404
        return Response(student_card(student))


class StudentSummaryView(SchoolAPIView):
    """Everything the home screen needs for one child, in one request."""

    def get(self, request, student_id):
        from apps.attendance.services import student_term_percent, student_today
        from apps.fees.models import FeeInvoice
        from apps.homework.models import Homework, HomeworkSubmission
        from apps.results.services import student_results
        from apps.transport.services import student_bus_summary

        student = student_for_request(request, student_id)
        today = school_today(request.school)

        invoices = [i for i in FeeInvoice.objects.filter(student=student) if i.balance > 0]
        next_invoice = min(invoices, key=lambda i: i.due_date) if invoices else None

        submitted = set(HomeworkSubmission.objects.filter(student=student).values_list("homework_id", flat=True))
        open_homework = [
            h
            for h in Homework.objects.filter(class_group=student.class_group, due_date__gte=today)
            .select_related("subject")
            .order_by("due_date")
            if h.id not in submitted
        ]

        remark = Remark.objects.filter(student=student).select_related("author").first()
        results = student_results(student)
        latest = results["exams"][0] if results["exams"] else None

        return Response(
            {
                "student": student_card(student),
                "attendance": {"today": student_today(student, today), "term_percent": student_term_percent(student)},
                "fees": {
                    "total_due": str(sum((i.balance for i in invoices), start=Decimal("0")).quantize(Decimal("0.01"))),
                    "next_due_date": next_invoice.due_date.isoformat() if next_invoice else None,
                    "overdue": any(i.due_date < today for i in invoices),
                },
                "homework": {
                    "pending": len(open_homework),
                    "next": {
                        "id": str(open_homework[0].id),
                        "title": open_homework[0].title,
                        "subject": open_homework[0].subject.name,
                        "due_date": open_homework[0].due_date.isoformat(),
                    }
                    if open_homework
                    else None,
                },
                "bus": student_bus_summary(student),
                "latest_remark": {
                    "body": remark.body,
                    "tone": remark.tone,
                    "author": remark.author.full_name,
                    "created_at": remark.created_at.isoformat(),
                }
                if remark
                else None,
                "latest_result": {"exam": latest["name"], "percent": latest["percent"], "grade": latest["grade"]}
                if latest
                else None,
                "trend": results["trend"],
            }
        )


class StudentTimetableView(SchoolAPIView):
    def get(self, request, student_id):
        student = student_for_request(request, student_id)
        return Response(_timetable_for(student.class_group, school_today(request.school)))


def _timetable_for(group: ClassGroup, today) -> dict:
    slots = TimetableSlot.objects.filter(class_group=group).select_related("subject", "teacher")
    days: dict = {}
    for slot in slots:
        days.setdefault(slot.weekday, []).append(
            {
                "period": slot.period,
                "starts_at": slot.starts_at.strftime("%H:%M"),
                "ends_at": slot.ends_at.strftime("%H:%M"),
                "subject": slot.subject.name,
                "color": slot.subject.color,
                "teacher": slot.teacher.full_name if slot.teacher else None,
                "room": slot.room,
            }
        )
    return {
        "class": group.label,
        "today": today.weekday(),
        "days": [{"weekday": d, "name": WEEKDAY_NAMES[d], "periods": days.get(d, [])} for d in range(6)],
    }


class RemarkSerializer(serializers.Serializer):
    body = serializers.CharField(max_length=1000)
    tone = serializers.ChoiceField(choices=["positive", "concern", "info"], default="positive")


class StudentRemarksView(SchoolAPIView):
    def get(self, request, student_id):
        student = student_for_request(request, student_id)
        remarks = Remark.objects.filter(student=student).select_related("author")[:30]
        return Response(
            {
                "items": [
                    {
                        "id": str(r.id),
                        "body": r.body,
                        "tone": r.tone,
                        "author": r.author.full_name,
                        "created_at": r.created_at.isoformat(),
                    }
                    for r in remarks
                ]
            }
        )

    def post(self, request, student_id):
        if not (request.roles & ({Role.TEACHER} | MANAGEMENT_ROLES)):
            raise PermissionDenied()
        student = student_for_request(request, student_id)
        data = RemarkSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        remark = Remark.objects.create(student=student, author=request.user, **data.validated_data)
        notify(
            list(guardians_of([student]).keys()),
            school=request.school,
            category=Category.GENERAL,
            title=f"A note about {student.first_name} from {request.user.full_name}",
            body=remark.body[:180],
            data={"student_id": str(student.id), "type": "remark"},
            dedupe_key=f"remark:{remark.id}",
        )
        return Response({"id": str(remark.id)}, status=status.HTTP_201_CREATED)


class TeacherClassesView(SchoolAPIView):
    allowed_roles = frozenset({Role.TEACHER}) | MANAGEMENT_ROLES

    def get(self, request):
        from apps.attendance.models import AttendanceSession

        today = school_today(request.school)
        ids = teacher_class_ids(request.user)
        groups = ClassGroup.objects.filter(id__in=ids).select_related("class_teacher")
        subjects: dict = {}
        for assignment in TeachingAssignment.objects.filter(teacher=request.user).select_related("subject"):
            subjects.setdefault(assignment.class_group_id, []).append(
                {"id": str(assignment.subject_id), "name": assignment.subject.name}
            )
        marked = set(
            AttendanceSession.objects.filter(class_group__in=groups, date=today).values_list("class_group_id", flat=True)
        )
        return Response(
            {
                "classes": [
                    {
                        "id": str(g.id),
                        "label": g.label,
                        "short_label": g.short_label,
                        "is_class_teacher": g.class_teacher_id == request.user.id,
                        "subjects": subjects.get(g.id, []),
                        "student_count": Student.objects.filter(class_group=g, is_active=True).count(),
                        "attendance_marked_today": g.id in marked,
                    }
                    for g in groups
                ]
            }
        )


class TeacherTodayView(SchoolAPIView):
    allowed_roles = frozenset({Role.TEACHER})

    def get(self, request):
        today = school_today(request.school)
        slots = (
            TimetableSlot.objects.filter(teacher=request.user, weekday=today.weekday())
            .select_related("subject", "class_group")
            .order_by("period")
        )
        return Response(
            {
                "date": today.isoformat(),
                "weekday": WEEKDAY_NAMES[today.weekday()],
                "periods": [
                    {
                        "period": s.period,
                        "starts_at": s.starts_at.strftime("%H:%M"),
                        "ends_at": s.ends_at.strftime("%H:%M"),
                        "subject": s.subject.name,
                        "color": s.subject.color,
                        "class": {"id": str(s.class_group_id), "label": s.class_group.label},
                        "room": s.room,
                    }
                    for s in slots
                ],
            }
        )
