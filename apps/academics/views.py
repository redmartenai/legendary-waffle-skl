from decimal import Decimal

from django.db.models import Q
from django.utils import timezone

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
        "house": student.house or None,
    }


class ParentChildrenView(SchoolAPIView):
    """A parent's children. `?detail=1` adds each child's day, month, latest exam, bus and fees (My Children)."""

    allowed_roles = frozenset({Role.PARENT})

    def get(self, request):
        children = list(children_of(request.user))
        if request.query_params.get("detail") != "1":
            return Response({"children": [student_card(s) for s in children]})
        return Response({"children": [child_detail(s, school_today(request.school)) for s in children]})


def child_detail(student: Student, today) -> dict:
    from apps.attendance.services import student_month
    from apps.fees.models import FeeInvoice
    from apps.results.services import student_results
    from apps.transport.models import StudentTransport

    from .today import check_in

    month = student_month(student, today.year, today.month, today)["summary"]
    results = student_results(student)
    latest = results["exams"][0] if results["exams"] else None
    ride = StudentTransport.objects.filter(student=student, is_active=True).select_related("route__vehicle", "drop_stop").first()
    outstanding = [i for i in FeeInvoice.objects.filter(student=student) if i.balance > 0]
    next_invoice = min(outstanding, key=lambda i: i.due_date) if outstanding else None
    group = student.class_group
    return {
        **student_card(student),
        "class_teacher": group.class_teacher.full_name if group.class_teacher else None,
        "today": check_in(student, today),
        "month": {"present": month["present"], "school_days": month["school_days"], "absent": month["absent"], "late": month["late"]},
        "latest_exam": {"name": latest["name"], "percent": latest["percent"], "grade": latest["grade"]} if latest else None,
        "transport": {
            "mode": "bus",
            "bus": ride.route.vehicle.label if ride.route.vehicle else ride.route.name,
            "stop": ride.drop_stop.name,
        }
        if ride
        else {"mode": "walking"},
        "next_invoice": {
            "id": str(next_invoice.id),
            "title": next_invoice.title,
            "amount": str(next_invoice.balance),
            "due_date": next_invoice.due_date.isoformat(),
        }
        if next_invoice
        else None,
    }


class StudentSelfView(SchoolAPIView):
    allowed_roles = frozenset({Role.STUDENT})

    def get(self, request):
        student = Student.objects.filter(user=request.user, is_active=True).select_related("class_group").first()
        if student is None:
            raise Http404
        return Response({**student_card(student), "id_card": id_card(student)})


def id_card(student: Student) -> dict:
    """What the student ID card shows: house, date of birth, bus, validity, principal."""
    from apps.accounts.models import Membership
    from apps.transport.models import StudentTransport

    from .models import AcademicYear

    ride = StudentTransport.objects.filter(student=student, is_active=True).select_related("route", "drop_stop").first()
    year = AcademicYear.objects.filter(is_current=True).first()
    principal = Membership.objects.filter(school=student.school, role=Role.PRINCIPAL, is_active=True).select_related("user").first()
    guardians = [g.user.full_name for g in student.guardian_links.select_related("user").filter(is_primary=True)]
    return {
        "house": student.house or None,
        "date_of_birth": student.date_of_birth.isoformat() if student.date_of_birth else None,
        "bus": {"route": ride.route.name, "stop": ride.drop_stop.name} if ride else None,
        "valid_till": year.ends_on.isoformat() if year else None,
        "academic_year": year.name if year else None,
        "principal": principal.user.full_name if principal else None,
        "guardian": guardians[0] if guardians else None,
        # The gate scanner looks the student up by this; it holds no personal data.
        "qr": f"EDUFLOW:{student.school.code}:{student.id}",
    }


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

        remark = Remark.objects.filter(student=student, visibility=Remark.Visibility.FAMILY).select_related("author").first()
        results = student_results(student)
        latest = results["exams"][0] if results["exams"] else None

        from apps.announcements.models import Announcement
        from apps.fees.models import FeeInvoiceItem

        from .today import check_in, day_ribbon, home_time

        items = list(FeeInvoiceItem.objects.filter(invoice=next_invoice)) if next_invoice else []
        now = timezone.now()
        event = (
            Announcement.objects.filter(
                event_starts_at__gte=now,
                published_at__lte=now,
                audience__in=[Announcement.Audience.EVERYONE, Announcement.Audience.FAMILIES, Announcement.Audience.CLASSES],
            )
            .filter(Q(audience__in=[Announcement.Audience.EVERYONE, Announcement.Audience.FAMILIES]) | Q(class_groups=student.class_group))
            .order_by("event_starts_at")
            .distinct()
            .first()
        )

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
                    "id": str(remark.id),
                    "body": remark.body,
                    "tone": remark.tone,
                    "author": remark.author.full_name,
                    "author_id": str(remark.author_id),
                    "author_initials": remark.author.initials,
                    "created_at": remark.created_at.isoformat(),
                }
                if remark
                else None,
                "today": {
                    "check_in": check_in(student, today),
                    "ribbon": day_ribbon(student, today),
                    "home": home_time(student, today),
                },
                "next_invoice": {
                    "id": str(next_invoice.id),
                    "title": next_invoice.title,
                    "amount": str(next_invoice.balance),
                    "due_date": next_invoice.due_date.isoformat(),
                    "heads": [i.head for i in items],
                }
                if next_invoice
                else None,
                "next_event": {
                    "id": str(event.id),
                    "title": event.title,
                    "body": event.body,
                    "kind": event.kind,
                    "starts_at": event.event_starts_at.isoformat(),
                    "ends_at": event.event_ends_at.isoformat() if event.event_ends_at else None,
                    "location": event.location,
                }
                if event
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
                "short": slot.subject.short_name,
                "code": slot.subject.code,
                "color": slot.subject.color,
                "teacher": slot.teacher.full_name if slot.teacher else None,
                "room": slot.room,
                "note": slot.note,
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
    visibility = serializers.ChoiceField(choices=Remark.Visibility.choices, default=Remark.Visibility.FAMILY)
    homework_id = serializers.UUIDField(required=False, allow_null=True)


FAMILY_ONLY = frozenset({Role.PARENT, Role.STUDENT})


def remark_payload(remark: Remark, acks: dict, subjects: dict, class_teacher_id=None) -> dict:
    ack = acks.get(remark.id)
    return {
        "id": str(remark.id),
        "body": remark.body,
        "tone": remark.tone,
        "visibility": remark.visibility,
        "author": remark.author.full_name,
        "author_id": str(remark.author_id),
        "author_initials": remark.author.initials,
        "author_subject": subjects.get(remark.author_id),
        "author_is_class_teacher": remark.author_id == class_teacher_id,
        "created_at": remark.created_at.isoformat(),
        "requires_ack": remark.requires_ack,
        "acknowledged_at": ack.isoformat() if ack else None,
        "homework": {"id": str(remark.homework_id), "title": remark.homework.title, "due_date": remark.homework.due_date.isoformat()}
        if remark.homework_id
        else None,
    }


class StudentRemarksView(SchoolAPIView):
    def get(self, request, student_id):
        from .models import RemarkAck

        student = student_for_request(request, student_id)
        remarks = Remark.objects.filter(student=student).select_related("author", "homework")
        # Families never see staff-only notes.
        if request.roles & FAMILY_ONLY and not request.roles - FAMILY_ONLY:
            remarks = remarks.filter(visibility=Remark.Visibility.FAMILY)
        remarks = list(remarks[:30])
        acks = {
            a.remark_id: a.created_at
            for a in RemarkAck.objects.filter(remark__in=remarks, user=request.user)
        }
        subjects: dict = {}
        for a in TeachingAssignment.objects.filter(class_group=student.class_group).select_related("subject"):
            subjects.setdefault(a.teacher_id, a.subject.name)
        teacher_id = student.class_group.class_teacher_id
        return Response({"items": [remark_payload(r, acks, subjects, teacher_id) for r in remarks]})

    def post(self, request, student_id):
        if not (request.roles & ({Role.TEACHER} | MANAGEMENT_ROLES)):
            raise PermissionDenied()
        student = student_for_request(request, student_id)
        data = RemarkSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        fields = dict(data.validated_data)
        homework_id = fields.pop("homework_id", None)
        remark = Remark.objects.create(
            student=student,
            author=request.user,
            homework_id=homework_id,
            requires_ack=fields["tone"] == "concern",
            **fields,
        )
        if remark.visibility == Remark.Visibility.STAFF:
            return Response({"id": str(remark.id)}, status=status.HTTP_201_CREATED)
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


class RemarkAckView(SchoolAPIView):
    """A family member confirms they have read a remark."""

    allowed_roles = FAMILY_ONLY

    def post(self, request, remark_id):
        from .models import RemarkAck

        remark = Remark.objects.filter(id=remark_id, visibility=Remark.Visibility.FAMILY).first()
        if remark is None:
            raise Http404
        student_for_request(request, remark.student_id)
        ack, _ = RemarkAck.objects.get_or_create(remark=remark, user=request.user)
        return Response({"id": str(remark.id), "acknowledged_at": ack.created_at.isoformat()})


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
