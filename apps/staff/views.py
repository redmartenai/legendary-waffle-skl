"""The staff (teacher) app: home, classes, timetable with covers, leave, documents and a student's profile."""

from collections import Counter
from datetime import date, timedelta
from decimal import Decimal

from django.db.models import Avg
from django.http import Http404
from django.utils import timezone
from rest_framework import status
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response

from apps.academics.access import can_manage_class
from apps.academics.models import ClassGroup, Remark, Student, StudentGuardian, TeachingAssignment, TimetableSlot
from apps.accounts.models import MANAGEMENT_ROLES, Membership, Role
from apps.core.api import SchoolAPIView
from apps.core.utils import mask_phone, school_now, school_today

from . import services
from .models import StaffLeave, StaffProfile, Substitution

TEACHING_ROLES = frozenset({Role.TEACHER}) | MANAGEMENT_ROLES
STAFF_APP_ROLES = frozenset({Role.TEACHER, Role.ACCOUNTANT, Role.TRANSPORT_MANAGER}) | MANAGEMENT_ROLES


# ------------------------------------------------------------------ helpers


def _register(group: ClassGroup, day: date) -> dict:
    """Today's roll call for a class: one status per roll number, plus who's away."""
    from apps.attendance.models import AttendanceException, AttendanceSession

    students = list(Student.objects.filter(class_group=group, is_active=True).order_by("roll_no", "full_name"))
    session = AttendanceSession.objects.filter(class_group=group, date=day).first()
    statuses = {e.student_id: e.status for e in AttendanceException.objects.filter(session=session)} if session else {}
    rolls = [
        {"id": str(s.id), "roll_no": s.roll_no, "name": s.full_name, "first_name": s.first_name, "status": statuses.get(s.id, "present") if session else None}
        for s in students
    ]
    tally = Counter(r["status"] for r in rolls if r["status"])
    return {
        "class": {"id": str(group.id), "short_label": group.short_label},
        "marked": session is not None,
        "marked_at": session.marked_at.isoformat() if session else None,
        "total": len(students),
        "present": tally["present"] + tally["late"] + tally["half_day"],
        "on_time": tally["present"],
        "absent": [r for r in rolls if r["status"] in ("absent", "excused")],
        "late": [r for r in rolls if r["status"] == "late"],
        "rolls": [{"id": r["id"], "roll_no": r["roll_no"], "status": r["status"]} for r in rolls],
    }


def _subject_exams(group: ClassGroup, subject_id) -> list[dict]:
    """Class average (as a %) for one subject in each published exam, oldest first."""
    from apps.results.models import Exam, ExamMark

    out = []
    for exam in Exam.objects.filter(class_group=group, is_published=True).order_by("held_on"):
        row = ExamMark.objects.filter(exam=exam, subject_id=subject_id, is_absent=False).aggregate(avg=Avg("marks"), out_of=Avg("max_marks"))
        if row["avg"] is not None and row["out_of"]:
            out.append({"id": str(exam.id), "name": exam.name, "average": round(float(row["avg"]) * 100 / float(row["out_of"]), 1)})
    return out


def _open_sheets(user) -> list:
    from apps.results.models import MarkSheet

    pairs = set(TeachingAssignment.objects.filter(teacher=user).values_list("class_group_id", "subject_id"))
    return [
        s
        for s in MarkSheet.objects.filter(status=MarkSheet.Status.OPEN, exam__is_published=False, exam__class_group_id__in={c for c, _ in pairs})
        .select_related("exam", "exam__class_group", "subject")
        .order_by("due_on")
        if (s.exam.class_group_id, s.subject_id) in pairs
    ]


def _office_hours(request) -> dict | None:
    membership = Membership.objects.filter(user=request.user, is_active=True).exclude(role__in=[Role.PARENT, Role.STUDENT]).order_by("created_at").first()
    return (membership.settings or {}).get("office_hours") if membership else None


# ------------------------------------------------------------------ classes


class StaffClassesView(SchoolAPIView):
    """The teacher's sections: size, room, today's register, their subject's unit-test averages and syllabus."""

    allowed_roles = TEACHING_ROLES

    def get(self, request):
        from apps.learning.models import SyllabusProgress
        from apps.results.marks import sheet_card

        today = school_today(request.school)
        sheets = {s.exam.class_group_id: s for s in _open_sheets(request.user)}
        cards = []
        for group in services.taught_classes(request.user):
            assignment = TeachingAssignment.objects.filter(teacher=request.user, class_group=group).select_related("subject").first()
            subject = assignment.subject if assignment else None
            slots = list(TimetableSlot.objects.filter(teacher=request.user, class_group=group).values_list("room", flat=True))
            progress = SyllabusProgress.objects.filter(class_group=group, subject=subject).first() if subject else None
            cards.append(
                {
                    "id": str(group.id),
                    "label": group.label,
                    "short_label": group.short_label,
                    "is_class_teacher": group.class_teacher_id == request.user.id,
                    "student_count": Student.objects.filter(class_group=group, is_active=True).count(),
                    "subject": {"id": str(subject.id), "name": subject.name, "code": subject.code, "short": subject.short_name} if subject else None,
                    "room": Counter(r for r in slots if r).most_common(1)[0][0] if any(slots) else "",
                    "periods_per_week": len(slots),
                    "syllabus": progress.percent if progress else None,
                    "current_topic": progress.current_topic if progress else "",
                    "today": _register(group, today),
                    "exams": _subject_exams(group, subject.id)[-2:] if subject else [],
                    "open_sheet": sheet_card(sheets[group.id]) if group.id in sheets else None,
                }
            )
        return Response({"subject": cards[0]["subject"] if cards else None, "classes": cards})


# ------------------------------------------------------------------ timetable


class StaffTimetableView(SchoolAPIView):
    allowed_roles = TEACHING_ROLES

    def get(self, request):
        today = school_today(request.school)
        raw = request.query_params.get("date")
        try:
            day = date.fromisoformat(raw) if raw else today
        except ValueError as exc:
            raise ValidationError({"date": "Use YYYY-MM-DD."}) from exc
        monday = day - timedelta(days=day.weekday())
        week = [monday + timedelta(days=i) for i in range(6)]
        weekly = TimetableSlot.objects.filter(teacher=request.user).count()
        covers = Substitution.objects.filter(teacher=request.user, date__range=(week[0], week[-1])).count()
        return Response(
            {
                "today": today.isoformat(),
                "week": [d.isoformat() for d in week],
                "periods_this_week": weekly + covers,
                "covers_this_week": covers,
                "classes": [g.short_label for g in services.taught_classes(request.user)],
                "day": services.teacher_day(request.user, request.school, day),
            }
        )


class CoverNoteView(SchoolAPIView):
    """The covering teacher leaves a handover note for the colleague they covered."""

    allowed_roles = TEACHING_ROLES

    def post(self, request, cover_id):
        from apps.notifications.models import Category
        from apps.notifications.services import notify

        sub = Substitution.objects.filter(id=cover_id, teacher=request.user).select_related("slot__class_group", "slot__subject", "absent_teacher").first()
        if sub is None:
            raise Http404
        note = str(request.data.get("note", "")).strip()
        if not note:
            raise ValidationError({"note": "Write a short note."})
        sub.handover_note = note[:1000]
        sub.handover_at = timezone.now()
        sub.save(update_fields=["handover_note", "handover_at", "updated_at"])
        if sub.absent_teacher:
            notify(
                [sub.absent_teacher],
                school=request.school,
                category=Category.GENERAL,
                title=f"Handover from {request.user.full_name}",
                body=f"{sub.slot.class_group.short_label} {sub.slot.subject.name}, P{sub.slot.period}: {note[:120]}",
                data={"type": "cover_note", "cover_id": str(sub.id)},
            )
        return Response(services._cover_payload(sub))


# ------------------------------------------------------------------ home


class StaffHomeView(SchoolAPIView):
    """Everything the teacher's home screen shows, in one request."""

    allowed_roles = TEACHING_ROLES

    def get(self, request):
        from apps.announcements.models import Announcement, AnnouncementAck
        from apps.homework.models import Homework, HomeworkSubmission
        from apps.messaging.models import ConversationMember, Meeting, Message
        from apps.results.marks import sheet_card

        user = request.user
        today = school_today(request.school)
        now = school_now(request.school)
        day = services.teacher_day(user, request.school, today)

        # Tomorrow's first class, and homework due in it.
        tomorrow_day = services.next_school_day(today)
        first = next((c for c in services.teacher_day(user, request.school, tomorrow_day)["cells"] if c.get("kind") in ("class", "cover")), None)
        tomorrow = None
        if first:
            due = Homework.objects.filter(class_group_id=first["class"]["id"], due_date=tomorrow_day, subject__name=first["subject"]).first()
            tomorrow = {"date": tomorrow_day.isoformat(), **first, "due": due.title if due else None}

        # The marking pile: my homework with the most hand-ins waiting.
        pairs = set(TeachingAssignment.objects.filter(teacher=user).values_list("class_group_id", "subject_id"))
        waiting = Counter(
            HomeworkSubmission.objects.filter(homework__class_group_id__in={c for c, _ in pairs}, status=HomeworkSubmission.Status.SUBMITTED)
            .values_list("homework_id", flat=True)
        )
        pile = None
        if waiting:
            homework = Homework.objects.filter(id__in=waiting.keys()).select_related("subject", "class_group")
            homework = [h for h in homework if (h.class_group_id, h.subject_id) in pairs]
            if homework:
                top = max(homework, key=lambda h: waiting[h.id])
                pile = {
                    "id": str(top.id),
                    "title": top.title,
                    "description": top.description,
                    "class": {"id": str(top.class_group_id), "short_label": top.class_group.short_label},
                    "due_date": top.due_date.isoformat(),
                    "to_review": waiting[top.id],
                    "total_to_review": sum(waiting[h.id] for h in homework),
                }

        # The class-teacher register.
        led = ClassGroup.objects.filter(class_teacher=user).first()
        register = _register(led, today) if led else None

        sheets = _open_sheets(user)

        # The latest staff notice that still wants an answer from me (or anything posted to staff in the last 3 days).
        acked = set(AnnouncementAck.objects.filter(user=user).values_list("announcement_id", flat=True))
        notices = Announcement.objects.filter(audience__in=[Announcement.Audience.STAFF, Announcement.Audience.EVERYONE], published_at__lte=now).select_related("created_by")
        notice = next((a for a in notices if a.requires_ack and a.id not in acked), None) or notices.filter(published_at__gte=now - timedelta(days=3)).first()

        # Parents who wrote and haven't been answered, and PTM slot requests.
        mine = ConversationMember.objects.filter(user=user, side=ConversationMember.Side.STAFF)
        parents = []
        for member in mine.select_related("conversation"):
            last = Message.objects.filter(conversation=member.conversation, deleted_at__isnull=True).order_by("-created_at").select_related("sender").first()
            if last and last.sender_id != user.id and (member.last_read_at is None or last.created_at > member.last_read_at):
                if ConversationMember.objects.filter(conversation=member.conversation, user=last.sender, side=ConversationMember.Side.FAMILY).exists():
                    parents.append(last.sender)
        requests = list(
            Meeting.objects.filter(conversation__members__user=user, status=Meeting.Status.REQUESTED, starts_at__gte=now).order_by("starts_at")
        )
        cover_done = [c for c in day["cells"] if c.get("kind") == "cover" and c["state"] == "done"]
        casual = next(b for b in services.leave_balances(user, request.school) if b["kind"] == "casual")
        return Response(
            {
                "now": now.isoformat(),
                "day": day,
                "tomorrow": tomorrow,
                "pile": pile,
                "register": register,
                "marks": sheet_card(sheets[0]) if sheets else None,
                "notice": {
                    "id": str(notice.id),
                    "title": notice.title,
                    "body": notice.body,
                    "kind": notice.kind,
                    "author": notice.created_by.full_name if notice.created_by else None,
                    "author_initials": notice.created_by.initials if notice.created_by else "",
                    "published_at": notice.published_at.isoformat(),
                    "requires_ack": notice.requires_ack,
                    "acknowledged": notice.id in acked,
                }
                if notice
                else None,
                "parents": {"count": len(parents), "names": [p.full_name for p in parents[:3]], "initials": [p.initials for p in parents[:3]]},
                "ptm": {
                    "count": len(requests),
                    "date": requests[0].starts_at.isoformat() if requests else None,
                    "title": requests[0].title if requests else None,
                },
                "covers_done": [{"class": c["class"]["short_label"], "subject": c["subject"]} for c in cover_done],
                "casual_left": casual["left"],
            }
        )


# ------------------------------------------------------------------ me


class StaffMeView(SchoolAPIView):
    allowed_roles = STAFF_APP_ROLES

    def get(self, request):
        profile = StaffProfile.objects.filter(user=request.user).first()
        pending = StaffLeave.objects.filter(user=request.user, status=StaffLeave.Status.PENDING).count()
        return Response(
            {
                "profile": {
                    "employee_id": profile.employee_id if profile else "",
                    "designation": profile.designation if profile else "",
                    "joined_on": profile.joined_on.isoformat() if profile and profile.joined_on else None,
                },
                "classes": [{"id": str(g.id), "short_label": g.short_label, "is_class_teacher": g.class_teacher_id == request.user.id} for g in services.taught_classes(request.user)],
                "leave": services.leave_balances(request.user, request.school),
                "pending_leave": pending,
                "periods_per_week": sum(services.periods_by_weekday(request.user)),
                "office_hours": _office_hours(request),
            }
        )

    def patch(self, request):
        hours = request.data.get("office_hours")
        membership = Membership.objects.filter(user=request.user, is_active=True).exclude(role__in=[Role.PARENT, Role.STUDENT]).order_by("created_at").first()
        if membership is None:
            raise PermissionDenied()
        if hours is not None:
            try:
                start, end = hours["start"], hours["end"]
                days = sorted({int(d) for d in hours.get("days", [0, 1, 2, 3, 4])})
                if not (0 <= days[0] and days[-1] <= 6) or start >= end or len(start) != 5 or len(end) != 5:
                    raise ValueError
            except (KeyError, TypeError, ValueError, IndexError) as exc:
                raise ValidationError({"office_hours": "Pick days and a start time before the end time."}) from exc
            settings_ = dict(membership.settings or {})
            settings_["office_hours"] = {"days": days, "start": start, "end": end}
            membership.settings = settings_
            membership.save(update_fields=["settings"])
        return self.get(request)


# ------------------------------------------------------------------ leave


class StaffLeaveView(SchoolAPIView):
    allowed_roles = STAFF_APP_ROLES
    parser_classes = [JSONParser, MultiPartParser, FormParser]

    def get(self, request):
        year = services.leave_year()
        principal = Membership.objects.filter(role=Role.PRINCIPAL, is_active=True).select_related("user").first()
        return Response(
            {
                "year": {"name": year.name, "starts_on": year.starts_on.isoformat(), "ends_on": year.ends_on.isoformat()} if year else None,
                "approver": principal.user.full_name if principal else None,
                "balances": services.leave_balances(request.user, request.school),
                "periods_by_weekday": services.periods_by_weekday(request.user),
                "requests": [_leave_payload(r) for r in StaffLeave.objects.filter(user=request.user).select_related("decided_by")[:30]],
            }
        )

    def post(self, request):
        kind = request.data.get("kind")
        if kind not in StaffLeave.Kind.values:
            raise ValidationError({"kind": "Choose casual, sick or earned leave."})
        try:
            start = date.fromisoformat(str(request.data.get("from_date")))
            end = date.fromisoformat(str(request.data.get("to_date") or request.data.get("from_date")))
        except ValueError as exc:
            raise ValidationError({"from_date": "Pick the dates."}) from exc
        half_day = str(request.data.get("half_day", "")).lower() in ("1", "true", "yes")
        reason = str(request.data.get("reason", "")).strip()
        today = school_today(request.school)
        if end < start:
            raise ValidationError({"to_date": "The last day can't be before the first."})
        if start < today:
            raise ValidationError({"from_date": "Leave can't start in the past. Talk to the office for past days."})
        if half_day and end != start:
            raise ValidationError({"half_day": "A half day is a single date."})
        if not reason:
            raise ValidationError({"reason": "Add a short reason."})
        days = services.working_days(start, end, half_day)
        if not days:
            raise ValidationError({"to_date": "Those dates have no working days."})
        balance = next(b for b in services.leave_balances(request.user, request.school) if b["kind"] == kind)
        if Decimal(str(balance["left"])) - Decimal(str(balance["pending"])) < days:
            raise ValidationError({"kind": f"You have {balance['left']:g} {kind} days left ({balance['pending']:g} already requested)."})
        certificate = request.FILES.get("certificate")
        if kind == StaffLeave.Kind.SICK and days > 2 and certificate is None:
            raise ValidationError({"certificate": "Sick leave longer than 2 days needs a medical certificate."})
        if certificate is not None and (certificate.size > 10 * 1024 * 1024 or not (certificate.content_type or "").startswith(("image/", "application/pdf"))):
            raise ValidationError({"certificate": "Attach a photo or PDF, up to 10 MB."})
        overlap = StaffLeave.objects.filter(
            user=request.user, status__in=[StaffLeave.Status.PENDING, StaffLeave.Status.APPROVED], from_date__lte=end, to_date__gte=start
        )
        if overlap.exists():
            raise ValidationError({"from_date": "You already have leave on some of these days."})
        leave = StaffLeave.objects.create(
            user=request.user,
            kind=kind,
            from_date=start,
            to_date=end,
            half_day=half_day,
            days=days,
            reason=reason[:400],
            certificate=certificate or "",
            certificate_name=certificate.name[:120] if certificate else "",
        )
        from apps.approvals.services import open_request

        span = f"{start:%a %d %b}" if start == end else f"{start:%d}–{end:%d %b}"
        open_request(
            kind="leave",
            target=leave,
            requested_by=request.user,
            summary=f"{leave.get_kind_display()} · {span} · {days:g} day{'s' if days != 1 else ''}",
            due_on=start,
        )
        return Response(_leave_payload(leave), status=status.HTTP_201_CREATED)


class StaffLeaveCancelView(SchoolAPIView):
    allowed_roles = STAFF_APP_ROLES

    def post(self, request, leave_id):
        leave = StaffLeave.objects.filter(id=leave_id, user=request.user).first()
        if leave is None:
            raise Http404
        if leave.status != StaffLeave.Status.PENDING:
            raise ValidationError({"status": "Only a pending request can be withdrawn."})
        from apps.approvals.services import withdraw

        leave.status = StaffLeave.Status.CANCELLED
        leave.save(update_fields=["status", "updated_at"])
        withdraw(leave, request.user)
        return Response(_leave_payload(leave))


def _leave_payload(r: StaffLeave) -> dict:
    return {
        "id": str(r.id),
        "kind": r.kind,
        "from_date": r.from_date.isoformat(),
        "to_date": r.to_date.isoformat(),
        "half_day": r.half_day,
        "days": float(r.days),
        "reason": r.reason,
        "status": r.status,
        "created_at": r.created_at.isoformat(),
        "decided_by": r.decided_by.full_name if r.decided_by else None,
        "decided_at": r.decided_at.isoformat() if r.decided_at else None,
        "decision_note": r.decision_note,
        "certificate_name": r.certificate_name,
    }


# ------------------------------------------------------------------ documents


STAFF_UPLOAD_KINDS = {"lesson_plan", "question_paper", "certificate"}


def _doc_payload(doc, today, downloaded: set, user) -> dict:
    ext = doc.file.name.rsplit(".", 1)[-1].upper() if "." in doc.file.name else ""
    locked = bool(doc.locked_until and doc.locked_until > today)
    return {
        "id": str(doc.id),
        "kind": doc.kind,
        "title": doc.title,
        "subtitle": doc.subtitle,
        "size": doc.size,
        "ext": ext,
        "date": (doc.issued_on or doc.created_at.date()).isoformat(),
        "status": doc.status or None,
        "locked": locked,
        "locked_until": doc.locked_until.isoformat() if doc.locked_until else None,
        "subject": doc.subject.name if doc.subject_id else None,
        "owner": doc.owner.full_name if doc.owner_id else None,
        "mine": doc.owner_id == user.id,
        "new": doc.kind == "circular" and doc.id not in downloaded and (today - doc.created_at.date()).days <= 7,
        "download": None if locked else f"/documents/{doc.id}/file",
    }


class StaffDocumentsView(SchoolAPIView):
    """A teacher's own documents (lesson plans, question papers, payslips, certificates) and the staff library."""

    allowed_roles = STAFF_APP_ROLES
    parser_classes = [JSONParser, MultiPartParser, FormParser]

    def get(self, request):
        from apps.documents.models import Document, DocumentDownload

        today = school_today(request.school)
        user = request.user
        downloaded = set(DocumentDownload.objects.filter(user=user, document__isnull=False).values_list("document_id", flat=True))
        docs = Document.objects.select_related("subject", "owner")
        my_subjects = set(TeachingAssignment.objects.filter(teacher=user).values_list("subject_id", flat=True))
        staff_wide = docs.filter(audience__in=[Document.Audience.STAFF, Document.Audience.EVERYONE])
        if request.query_params.get("scope") == "school":
            sections = [
                ("circular", [d for d in staff_wide.filter(kind="circular")]),
                ("policy", [d for d in staff_wide.filter(kind__in=["policy", "other"])]),
                ("lesson_plan", [d for d in staff_wide.filter(kind="lesson_plan", subject_id__in=my_subjects).exclude(owner=user)]),
            ]
        else:
            sections = [
                ("lesson_plan", list(docs.filter(owner=user, kind="lesson_plan"))),
                ("question_paper", list(docs.filter(owner=user, kind="question_paper"))),
                ("circular", list(staff_wide.filter(kind="circular", created_at__date__gte=today - timedelta(days=45)))),
                ("payslip", list(docs.filter(owner=user, kind="payslip"))),
                ("certificate", list(docs.filter(owner=user, kind="certificate", audience=Document.Audience.PRIVATE))),
            ]
        return Response(
            {
                "sections": [
                    {"key": key, "items": [_doc_payload(d, today, downloaded, user) for d in items]}
                    for key, items in sections
                    if items or key in ("lesson_plan", "question_paper", "certificate")
                ]
            }
        )

    def post(self, request):
        from apps.documents.models import Document

        kind = request.data.get("kind")
        if kind not in STAFF_UPLOAD_KINDS:
            raise ValidationError({"kind": "Upload a lesson plan, question paper or certificate."})
        upload = request.FILES.get("file")
        if upload is None:
            raise ValidationError({"file": "Choose a file."})
        if upload.size > 20 * 1024 * 1024:
            raise ValidationError({"file": "Files can be up to 20 MB."})
        title = str(request.data.get("title") or upload.name.rsplit(".", 1)[0])[:160]
        subject = TeachingAssignment.objects.filter(teacher=request.user).select_related("subject").first()
        doc = Document.objects.create(
            kind=kind,
            title=title,
            subtitle=str(request.data.get("subtitle", ""))[:160],
            file=upload,
            size=upload.size,
            owner=request.user,
            subject=subject.subject if subject and kind == "lesson_plan" else None,
            # Lesson plans are shared with the department; papers go to the exam cell; certificates stay private.
            audience=Document.Audience.STAFF if kind == "lesson_plan" else Document.Audience.PRIVATE,
            status={"question_paper": "submitted", "certificate": "in_review"}.get(kind, ""),
            issued_on=school_today(request.school),
        )
        return Response(_doc_payload(doc, school_today(request.school), set(), request.user), status=status.HTTP_201_CREATED)


# ------------------------------------------------------------------ a student, as staff see them


class StaffStudentView(SchoolAPIView):
    """A student's profile for their teachers: card, parent contact (masked), marks in my subject, attendance, homework, remarks."""

    allowed_roles = TEACHING_ROLES

    def get(self, request, student_id):
        from apps.attendance.services import student_month
        from apps.homework.models import Homework, HomeworkSubmission
        from apps.transport.models import StudentTransport

        from apps.academics.views import remark_payload

        student = Student.objects.filter(id=student_id, is_active=True).select_related("class_group").first()
        if student is None or not can_manage_class(request, student.class_group):
            raise Http404
        group = student.class_group
        classmates = list(Student.objects.filter(class_group=group, is_active=True).order_by("roll_no", "full_name").values_list("id", flat=True))
        index = classmates.index(student.id)
        today = school_today(request.school)

        link = StudentGuardian.objects.filter(student=student).select_related("user").order_by("-is_primary").first()
        ride = StudentTransport.objects.filter(student=student, is_active=True).select_related("route").first()
        assignment = TeachingAssignment.objects.filter(teacher=request.user, class_group=group).select_related("subject").first()
        subject = assignment.subject if assignment else None

        exams = []
        if subject:
            from apps.results.models import ExamMark

            averages = {e["id"]: e["average"] for e in _subject_exams(group, subject.id)}
            for mark in ExamMark.objects.filter(student=student, subject=subject, exam__is_published=True, is_absent=False).select_related("exam").order_by("exam__held_on"):
                exams.append(
                    {"name": mark.exam.name, "percent": round(float(mark.marks) * 100 / float(mark.max_marks), 1), "class_average": averages.get(str(mark.exam_id))}
                )

        term_start = today - timedelta(days=120)
        homework = list(Homework.objects.filter(class_group=group, due_date__gte=term_start, due_date__lt=today).order_by("due_date"))
        handed = set(HomeworkSubmission.objects.filter(homework__in=homework, student=student).values_list("homework_id", flat=True))

        remarks = Remark.objects.filter(student=student).select_related("author", "homework")[:20]
        subjects: dict = {}
        for a in TeachingAssignment.objects.filter(class_group=group).select_related("subject"):
            subjects.setdefault(a.teacher_id, a.subject.name)
        return Response(
            {
                "student": {
                    "id": str(student.id),
                    "name": student.full_name,
                    "first_name": student.first_name,
                    "initials": student.initials,
                    "class": group.short_label,
                    "roll_no": student.roll_no,
                    "admission_no": student.admission_no,
                    "house": student.house or None,
                    "bus": ride.route.name if ride else None,
                },
                "position": {
                    "index": index + 1,
                    "total": len(classmates),
                    "previous": str(classmates[index - 1]) if index > 0 else None,
                    "next": str(classmates[index + 1]) if index + 1 < len(classmates) else None,
                },
                "is_class_teacher": group.class_teacher_id == request.user.id,
                "guardian": {
                    "user_id": str(link.user_id),
                    "name": link.user.full_name,
                    "initials": link.user.initials,
                    "relationship": link.relationship,
                    "phone": mask_phone(link.user.phone),
                }
                if link
                else None,
                "subject": {"name": subject.name, "exams": exams[-2:]} if subject else None,
                "attendance": student_month(student, today.year, today.month, today),
                "homework": {"handed_in": len(handed), "total": len(homework), "cells": [h.id in handed for h in homework]},
                "remarks": [{**remark_payload(r, {}, subjects, group.class_teacher_id), "mine": r.author_id == request.user.id} for r in remarks],
            }
        )


# ------------------------------------------------------------------ PTM requests


class MeetingDecisionView(SchoolAPIView):
    """The teacher accepts a requested PTM slot or proposes another time; the thread shows it."""

    allowed_roles = TEACHING_ROLES

    def post(self, request, meeting_id, action):
        from datetime import datetime

        from apps.messaging import services as chat
        from apps.messaging.models import ConversationMember, Meeting

        meeting = Meeting.objects.filter(id=meeting_id).select_related("conversation").first()
        if meeting is None or not ConversationMember.objects.filter(conversation=meeting.conversation, user=request.user, side=ConversationMember.Side.STAFF).exists():
            raise Http404
        if meeting.status != Meeting.Status.REQUESTED:
            raise ValidationError({"status": "This request has already been answered."})
        if action == "accept":
            meeting.status = Meeting.Status.BOOKED
            meeting.booked_by = request.user
            meeting.save(update_fields=["status", "booked_by", "updated_at"])
            text = f"Confirmed: {meeting.title}, {timezone.localtime(meeting.starts_at):%a %d %b, %I:%M %p}".replace(" 0", " ")
        elif action == "propose":
            try:
                starts = datetime.fromisoformat(str(request.data.get("starts_at")))
            except ValueError as exc:
                raise ValidationError({"starts_at": "Pick a time."}) from exc
            if timezone.is_naive(starts):
                starts = timezone.make_aware(starts)
            if starts <= timezone.now():
                raise ValidationError({"starts_at": "Pick a time in the future."})
            length = meeting.ends_at - meeting.starts_at
            meeting.starts_at, meeting.ends_at = starts, starts + length
            meeting.save(update_fields=["starts_at", "ends_at", "updated_at"])
            text = f"Could we meet at {timezone.localtime(starts):%a %d %b, %I:%M %p} instead?".replace(" 0", " ")
        else:
            raise Http404
        chat.send_message(meeting.conversation, request.user, text, client_id=f"meeting:{meeting.id}:{action}:{timezone.now().timestamp():.0f}")
        return Response(chat.meeting_payload(meeting))
