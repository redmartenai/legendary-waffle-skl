from datetime import timedelta

from django.http import FileResponse, Http404
from django.utils import timezone
from rest_framework import status
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response

from apps.academics.access import student_for_request
from apps.academics.models import Student, TeachingAssignment, TimetableSlot
from apps.accounts.models import MANAGEMENT_ROLES, Role
from apps.core.api import SchoolAPIView
from apps.core.utils import school_now, school_today

from .models import Assignment, AssignmentGroup, AssignmentSubmission, MilestoneProgress, StudyMaterial, SubmissionFile, SyllabusProgress

NEW_DAYS = 7
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _subject(subject) -> dict:
    return {"id": str(subject.id), "name": subject.name, "code": subject.code, "short": subject.short_name, "color": subject.color}


def material_payload(m: StudyMaterial, now) -> dict:
    return {
        "id": str(m.id),
        "kind": m.kind,
        "title": m.title,
        "description": m.description,
        "subject": _subject(m.subject),
        "author": m.author.full_name if m.author else None,
        "published_at": m.published_at.isoformat(),
        "size": m.size,
        "pages": m.pages,
        "duration_minutes": m.duration_minutes,
        "url": m.url or None,
        "download": f"/materials/{m.id}/file" if m.file else None,
        "new": (now - m.published_at) <= timedelta(days=NEW_DAYS),
    }


class StudentMaterialsView(SchoolAPIView):
    """Study material for a child's class. `?subject=MATH` filters by subject code."""

    def get(self, request, student_id):
        student = student_for_request(request, student_id)
        now = timezone.now()
        items = StudyMaterial.objects.filter(class_group=student.class_group).select_related("subject", "author")
        code = request.query_params.get("subject")
        all_items = list(items)
        shown = [m for m in all_items if not code or m.subject.code == code]
        new_by_subject: dict = {}
        for m in all_items:
            if (now - m.published_at) <= timedelta(days=NEW_DAYS):
                new_by_subject[m.subject.code] = new_by_subject.get(m.subject.code, 0) + 1
        subjects = {m.subject.code: _subject(m.subject) for m in all_items}
        return Response(
            {
                "items": [material_payload(m, now) for m in shown],
                "new_total": sum(new_by_subject.values()),
                "subjects": [{**s, "new": new_by_subject.get(c, 0)} for c, s in sorted(subjects.items(), key=lambda kv: -new_by_subject.get(kv[0], 0))],
            }
        )


class MaterialFileView(SchoolAPIView):
    def get(self, request, material_id):
        material = StudyMaterial.objects.filter(id=material_id).first()
        if material is None or not material.file:
            raise Http404
        allowed = request.roles - {Role.PARENT, Role.STUDENT}
        if not allowed:
            # Families only see their own children's classes.
            from apps.academics.access import children_of

            students = list(children_of(request.user)) + list(Student.objects.filter(user=request.user))
            if not any(s.class_group_id == material.class_group_id for s in students):
                raise Http404
        return FileResponse(material.file.open("rb"), as_attachment=True, filename=material.file.name.rsplit("/", 1)[-1])


class StudentClassesView(SchoolAPIView):
    """A child's subjects as a shelf: teacher, next class, syllabus done, new material."""

    def get(self, request, student_id):
        student = student_for_request(request, student_id)
        group = student.class_group
        now = school_now(request.school)
        today = now.date()
        slots = list(TimetableSlot.objects.filter(class_group=group).select_related("subject", "teacher"))
        progress = {p.subject_id: p for p in SyllabusProgress.objects.filter(class_group=group)}
        cutoff = timezone.now() - timedelta(days=NEW_DAYS)
        new_counts: dict = {}
        for m in StudyMaterial.objects.filter(class_group=group, published_at__gte=cutoff):
            new_counts[m.subject_id] = new_counts.get(m.subject_id, 0) + 1
        subjects = []
        for assignment in TeachingAssignment.objects.filter(class_group=group).select_related("subject", "teacher"):
            mine = sorted([s for s in slots if s.subject_id == assignment.subject_id], key=lambda s: (s.weekday, s.starts_at))
            now_slot = next((s for s in mine if s.weekday == today.weekday() and s.starts_at <= now.time() < s.ends_at), None)
            upcoming = None
            for offset in range(0, 8):
                day = today + timedelta(days=offset)
                for s in mine:
                    if s.weekday == day.weekday() and (offset > 0 or s.starts_at > now.time()):
                        upcoming = {"date": day.isoformat(), "weekday": WEEKDAYS[day.weekday()], "starts_at": s.starts_at.strftime("%H:%M"), "room": s.room}
                        break
                if upcoming:
                    break
            p = progress.get(assignment.subject_id)
            subjects.append(
                {
                    "subject": _subject(assignment.subject),
                    "teacher": assignment.teacher.full_name,
                    "teacher_id": str(assignment.teacher_id),
                    "is_class_teacher": assignment.teacher_id == group.class_teacher_id,
                    "now": {"ends_at": now_slot.ends_at.strftime("%H:%M"), "room": now_slot.room} if now_slot else None,
                    "next": upcoming,
                    "syllabus": p.percent if p else None,
                    "current_topic": p.current_topic if p else "",
                    "new_materials": new_counts.get(assignment.subject_id, 0),
                    "periods_per_week": len(mine),
                }
            )
        order = ["MATH", "ENG", "SCI", "HIN", "SST", "CS", "ART", "PE", "MUS"]
        subjects.sort(key=lambda s: order.index(s["subject"]["code"]) if s["subject"]["code"] in order else 99)
        percents = [s["syllabus"] for s in subjects if s["syllabus"] is not None and s["subject"]["code"] not in ("ART", "PE", "MUS")]
        return Response(
            {
                "class": group.short_label,
                "subjects": subjects,
                "new_total": sum(new_counts.values()),
                "syllabus_average": round(sum(percents) / len(percents)) if percents else None,
                "today": today.isoformat(),
            }
        )


def _group_for(assignment: Assignment, student: Student) -> AssignmentGroup | None:
    return AssignmentGroup.objects.filter(assignment=assignment, members=student).prefetch_related("members").first()


def assignment_payload(request, a: Assignment, student: Student, today) -> dict:
    group = _group_for(a, student)
    submission = (
        AssignmentSubmission.objects.filter(assignment=a, group=group).first()
        if group
        else AssignmentSubmission.objects.filter(assignment=a, student=student).first()
    )
    milestones = []
    if group:
        rows = {p.milestone_id: p for p in MilestoneProgress.objects.filter(group=group).prefetch_related("owners")}
        for m in a.milestones.all():
            p = rows.get(m.id)
            milestones.append(
                {
                    "id": str(m.id),
                    "progress_id": str(p.id) if p else None,
                    "title": m.title,
                    "due_date": m.due_date.isoformat(),
                    "done_at": p.done_at.isoformat() if p and p.done_at else None,
                    "owners": [{"id": str(o.id), "first_name": o.first_name, "me": o.id == student.id} for o in (p.owners.all() if p else [])],
                }
            )
    locked = bool(a.opens_on and a.opens_on > today)
    return {
        "id": str(a.id),
        "kind": a.kind,
        "title": a.title,
        "description": "" if locked else a.description,
        "teaser": a.teaser,
        "subject": _subject(a.subject),
        "teacher": a.created_by.full_name if a.created_by else None,
        "teacher_id": str(a.created_by_id) if a.created_by_id else None,
        "group_size": a.group_size,
        "max_marks": a.max_marks,
        "rubric": a.rubric,
        "opens_on": a.opens_on.isoformat() if a.opens_on else None,
        "locked": locked,
        "due_date": a.due_date.isoformat(),
        "group": {
            "id": str(group.id),
            "members": [{"id": str(s.id), "name": s.full_name, "first_name": s.first_name, "initials": s.initials, "me": s.id == student.id} for s in group.members.all()],
        }
        if group
        else None,
        "milestones": milestones,
        "submission": {
            "id": str(submission.id),
            "status": submission.status,
            "submitted_at": submission.submitted_at.isoformat(),
            "total": float(submission.total) if submission.total is not None else None,
            "grade": submission.grade,
            "feedback": submission.feedback,
            "graded_by": submission.graded_by.full_name if submission.graded_by else None,
            "graded_at": submission.graded_at.isoformat() if submission.graded_at else None,
            "files": [{"id": str(f.id), "name": f.name, "size": f.size} for f in submission.files.all()],
        }
        if submission
        else None,
    }


class StudentAssignmentsView(SchoolAPIView):
    def get(self, request, student_id):
        student = student_for_request(request, student_id)
        today = school_today(request.school)
        items = [
            assignment_payload(request, a, student, today)
            for a in Assignment.objects.filter(class_group=student.class_group).select_related("subject", "created_by").prefetch_related("milestones")
        ]
        graded = [a for a in items if a["submission"] and a["submission"]["status"] == "graded"]
        upcoming = [a for a in items if a["locked"]]
        in_progress = [a for a in items if a not in graded and a not in upcoming]
        return Response({"in_progress": in_progress, "graded": graded, "upcoming": upcoming})


class MilestoneToggleView(SchoolAPIView):
    """A group member ticks a milestone done (or un-ticks it)."""

    allowed_roles = frozenset({Role.STUDENT})

    def post(self, request, progress_id):
        progress = MilestoneProgress.objects.filter(id=progress_id).select_related("group").first()
        if progress is None or not progress.group.members.filter(user=request.user).exists():
            raise Http404
        done = request.data.get("done")
        progress.done_at = timezone.now() if done in (True, "true", "1", 1) else None
        progress.done_by = request.user if progress.done_at else None
        progress.save(update_fields=["done_at", "done_by", "updated_at"])
        return Response({"id": str(progress.id), "done_at": progress.done_at.isoformat() if progress.done_at else None})


class AssignmentUploadView(SchoolAPIView):
    """Hand in files for an assignment (a group's upload is shared by the group)."""

    parser_classes = [MultiPartParser, FormParser, JSONParser]
    allowed_roles = frozenset({Role.STUDENT, Role.PARENT})

    def post(self, request, assignment_id):
        assignment = Assignment.objects.filter(id=assignment_id).first()
        if assignment is None:
            raise Http404
        student = student_for_request(request, request.data.get("student_id"))
        if student.class_group_id != assignment.class_group_id:
            raise Http404
        if assignment.opens_on and assignment.opens_on > school_today(request.school):
            raise ValidationError({"assignment": "This assignment hasn't opened yet."})
        files = request.FILES.getlist("files")
        if not files:
            raise ValidationError({"files": "Add at least one photo or PDF."})
        for f in files:
            if f.size > MAX_UPLOAD_BYTES:
                raise ValidationError({"files": "Each file can be up to 10 MB."})
            if not (f.content_type or "").startswith(("image/", "application/pdf")):
                raise ValidationError({"files": "Upload photos or PDFs."})
        group = _group_for(assignment, student)
        submission, _ = AssignmentSubmission.objects.get_or_create(
            assignment=assignment,
            group=group,
            student=None if group else student,
            defaults={"submitted_by": request.user, "submitted_at": timezone.now()},
        )
        if submission.status == AssignmentSubmission.Status.GRADED:
            raise PermissionDenied("This work has already been graded.")
        submission.submitted_at = timezone.now()
        submission.submitted_by = request.user
        submission.status = AssignmentSubmission.Status.SUBMITTED
        submission.save(update_fields=["submitted_at", "submitted_by", "status", "updated_at"])
        for f in files:
            SubmissionFile.objects.create(submission=submission, file=f, name=f.name[:120], size=f.size)
        return Response(assignment_payload(request, assignment, student, school_today(request.school)), status=status.HTTP_201_CREATED)



# ---------------------------------------------------------------- teacher side


def _teaches(request, a: Assignment) -> bool:
    if request.roles & MANAGEMENT_ROLES:
        return True
    return a.created_by_id == request.user.id or TeachingAssignment.objects.filter(
        teacher=request.user, class_group=a.class_group, subject=a.subject
    ).exists()


def _people(submission: AssignmentSubmission) -> list:
    if submission.group_id:
        return list(submission.group.members.all())
    return [submission.student] if submission.student else []


def _review_row(request, s: AssignmentSubmission) -> dict:
    people = _people(s)
    lead = people[0] if people else None
    return {
        "id": str(s.id),
        "status": s.status,
        "submitted_at": s.submitted_at.isoformat(),
        "student": {"id": str(lead.id), "name": lead.full_name, "first_name": lead.first_name, "initials": lead.initials} if lead else None,
        "members": [p.full_name for p in people],
        "files": [{"id": str(f.id), "name": f.name, "size": f.size, "url": f"/assignments/files/{f.id}"} for f in s.files.all()],
        "scores": s.scores or {},
        "total": float(s.total) if s.total is not None else None,
        "grade": s.grade,
        "feedback": s.feedback,
        "graded_at": s.graded_at.isoformat() if s.graded_at else None,
    }


def assignment_card(a: Assignment, today) -> dict:
    subs = list(a.submissions.all())
    class_size = Student.objects.filter(class_group=a.class_group, is_active=True).count()
    groups = a.groups.count() if a.group_size > 1 else 0
    expected = groups or class_size
    return {
        "id": str(a.id),
        "title": a.title,
        "description": a.description,
        "kind": a.kind,
        "subject": _subject(a.subject),
        "class": {"id": str(a.class_group_id), "short_label": a.class_group.short_label},
        "group_size": a.group_size,
        "max_marks": a.max_marks,
        "rubric": a.rubric,
        "due_date": a.due_date.isoformat(),
        "days_left": (a.due_date - today).days,
        "counts": {
            "to_review": sum(1 for s in subs if s.status == AssignmentSubmission.Status.SUBMITTED),
            "graded": sum(1 for s in subs if s.status == AssignmentSubmission.Status.GRADED),
            "redo": sum(1 for s in subs if s.status == AssignmentSubmission.Status.REDO),
            "missing": max(0, expected - len(subs)),
            "expected": expected,
        },
    }


class TeacherAssignmentsView(SchoolAPIView):
    allowed_roles = frozenset({Role.TEACHER}) | MANAGEMENT_ROLES

    def get(self, request):
        pairs = set(TeachingAssignment.objects.filter(teacher=request.user).values_list("class_group_id", "subject_id"))
        today = school_today(request.school)
        items = [
            a
            for a in Assignment.objects.filter(class_group_id__in={c for c, _ in pairs})
            .select_related("subject", "class_group")
            .prefetch_related("submissions")
            .order_by("due_date")
            if (a.class_group_id, a.subject_id) in pairs or a.created_by_id == request.user.id
        ]
        return Response({"assignments": [assignment_card(a, today) for a in items]})


class AssignmentReviewView(SchoolAPIView):
    """The teacher's queue for one assignment: to review, graded (incl. redo), and who hasn't handed in."""

    allowed_roles = frozenset({Role.TEACHER}) | MANAGEMENT_ROLES

    def get(self, request, assignment_id):
        a = Assignment.objects.filter(id=assignment_id).select_related("subject", "class_group").first()
        if a is None or not _teaches(request, a):
            raise Http404
        subs = list(
            a.submissions.select_related("student", "group").prefetch_related("files", "group__members").order_by("submitted_at")
        )
        handed_in = {p.id for s in subs for p in _people(s)}
        missing = Student.objects.filter(class_group=a.class_group, is_active=True).exclude(id__in=handed_in).order_by("roll_no")
        return Response(
            {
                "assignment": assignment_card(a, school_today(request.school)),
                "to_review": [_review_row(request, s) for s in subs if s.status == AssignmentSubmission.Status.SUBMITTED],
                "graded": [_review_row(request, s) for s in subs if s.status != AssignmentSubmission.Status.SUBMITTED],
                "missing": [{"id": str(s.id), "name": s.full_name, "initials": s.initials, "roll_no": s.roll_no} for s in missing],
            }
        )


class SubmissionGradeView(SchoolAPIView):
    """Grade against the rubric (or return the work for a redo). Students and parents are told."""

    allowed_roles = frozenset({Role.TEACHER}) | MANAGEMENT_ROLES

    def post(self, request, submission_id):
        from apps.academics.access import guardians_of
        from apps.notifications.models import Category
        from apps.notifications.services import notify
        from apps.results.services import grade_for

        s = AssignmentSubmission.objects.filter(id=submission_id).select_related("assignment", "assignment__class_group", "assignment__subject", "student", "group").first()
        if s is None or not _teaches(request, s.assignment):
            raise Http404
        a = s.assignment
        action = request.data.get("action", "grade")
        feedback = str(request.data.get("feedback", "")).strip()[:1000]
        if action == "redo":
            if not feedback:
                raise ValidationError({"feedback": "Say what to redo."})
            s.status = AssignmentSubmission.Status.REDO
        elif action == "grade":
            raw = request.data.get("scores") or {}
            scores, errors = {}, {}
            for item in a.rubric or []:
                value = raw.get(item["key"])
                try:
                    value = float(value)
                except (TypeError, ValueError):
                    errors[item["key"]] = "Enter a score."
                    continue
                if not 0 <= value <= item["max"]:
                    errors[item["key"]] = f"Between 0 and {item['max']}."
                    continue
                scores[item["key"]] = value
            if errors:
                raise ValidationError({"scores": errors})
            if a.rubric:
                total = sum(scores.values())
            else:
                try:
                    total = float(request.data.get("total"))
                except (TypeError, ValueError) as exc:
                    raise ValidationError({"total": "Enter the marks."}) from exc
                if not 0 <= total <= a.max_marks:
                    raise ValidationError({"total": f"Between 0 and {a.max_marks}."})
            s.scores = scores
            s.total = total
            s.grade = grade_for(total * 100 / a.max_marks, request.school) if a.max_marks else ""
            s.status = AssignmentSubmission.Status.GRADED
        else:
            raise ValidationError({"action": "Use grade or redo."})
        s.feedback = feedback
        s.graded_by = request.user
        s.graded_at = timezone.now()
        s.save()
        people = _people(s)
        users = [p.user for p in people if p.user_id] + list(guardians_of(people).keys())
        title = f"{a.title}: graded" if s.status == AssignmentSubmission.Status.GRADED else f"{a.title}: please redo"
        body = (
            f"{a.subject.name} · {s.total:g}/{a.max_marks}, grade {s.grade}" if s.status == AssignmentSubmission.Status.GRADED else feedback[:120]
        )
        notify(users, school=request.school, category=Category.HOMEWORK, title=title, body=body, data={"type": "assignment", "assignment_id": str(a.id)})
        return Response(_review_row(request, s))


class SubmissionFileView(SchoolAPIView):
    """A handed-in file: the teacher, the student(s) who handed it in, and their parents."""

    def get(self, request, file_id):
        f = SubmissionFile.objects.filter(id=file_id).select_related("submission__assignment__class_group", "submission__assignment__subject", "submission__student", "submission__group").first()
        if f is None:
            raise Http404
        s = f.submission
        if not _teaches(request, s.assignment):
            people = _people(s)
            if not people:
                raise Http404
            for p in people:
                try:
                    student_for_request(request, p.id)
                    break
                except Http404:
                    continue
            else:
                raise Http404
        return FileResponse(f.file.open("rb"), as_attachment=True, filename=f.name)
