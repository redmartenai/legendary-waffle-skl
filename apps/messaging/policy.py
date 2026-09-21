"""Who may message whom. Schools can tune this in School.settings["chat"]."""

from __future__ import annotations

from datetime import datetime

from apps.academics.access import children_of, teacher_class_ids
from apps.academics.models import ClassGroup, Student, StudentGuardian, TeachingAssignment
from apps.accounts.models import Department, Membership, Role
from apps.core.utils import initials, parse_hhmm, time_in_window
from apps.transport.models import StudentTransport

WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
FAMILY_DEPARTMENTS = (Department.OFFICE, Department.ACCOUNTS, Department.TRANSPORT)


def office_hours_of(user) -> dict | None:
    membership = (
        Membership.objects.filter(user=user, is_active=True)
        .exclude(role__in=[Role.PARENT, Role.STUDENT])
        .order_by("created_at")
        .first()
    )
    return (membership.settings or {}).get("office_hours") if membership else None


def office_hours_open(hours: dict | None, now_local: datetime) -> bool:
    if not hours:
        return True
    if now_local.weekday() not in hours.get("days", range(7)):
        return False
    return time_in_window(now_local.time(), parse_hhmm(hours["start"]), parse_hhmm(hours["end"]))


def office_hours_text(hours: dict | None) -> str | None:
    if not hours:
        return None
    days = sorted(hours.get("days", []))
    if days and days == list(range(days[0], days[-1] + 1)) and len(days) > 1:
        day_text = f"{WEEKDAYS[days[0]]}–{WEEKDAYS[days[-1]]}"
    else:
        day_text = ", ".join(WEEKDAYS[d] for d in days)

    def fmt(value: str) -> str:
        t = parse_hhmm(value)
        return t.strftime("%I:%M %p").lstrip("0")

    return f"Usually replies {day_text}, {fmt(hours['start'])} – {fmt(hours['end'])}"


def _teacher_label(class_group: ClassGroup, subjects: list[str], is_class_teacher: bool) -> str:
    if is_class_teacher and subjects:
        return f"Class teacher · {', '.join(subjects)}"
    if is_class_teacher:
        return f"Class teacher · {class_group.short_label}"
    return ", ".join(subjects) or "Teacher"


def family_contacts(request) -> list[dict]:
    """Teachers and office teams a parent (or college student) may start a chat with."""
    school = request.school
    kids: list[Student] = []
    if Role.PARENT in request.roles:
        kids.extend(children_of(request.user))
    if Role.STUDENT in request.roles and school.policy("chat", "student_chat"):
        kids.extend(Student.objects.filter(user=request.user, is_active=True).select_related("class_group"))

    with_subject_teachers = school.policy("chat", "parent_to_subject_teachers")
    contacts: list[dict] = []
    for kid in kids:
        group = kid.class_group
        teachers: dict = {}
        if group.class_teacher_id:
            teachers.setdefault(group.class_teacher_id, {"user": group.class_teacher, "subjects": [], "ct": True})
        for assignment in TeachingAssignment.objects.filter(class_group=group).select_related("teacher", "subject"):
            is_class_teacher = assignment.teacher_id == group.class_teacher_id
            if not with_subject_teachers and not is_class_teacher:
                continue
            entry = teachers.setdefault(
                assignment.teacher_id, {"user": assignment.teacher, "subjects": [], "ct": is_class_teacher}
            )
            entry["subjects"].append(assignment.subject.name)
        for entry in sorted(teachers.values(), key=lambda e: (not e["ct"], e["user"].full_name)):
            contacts.append(
                {
                    "kind": "user",
                    "user_id": str(entry["user"].id),
                    "name": entry["user"].full_name,
                    "initials": entry["user"].initials,
                    "subtitle": _teacher_label(group, entry["subjects"], entry["ct"]),
                    "student": {"id": str(kid.id), "name": kid.full_name, "first_name": kid.first_name},
                }
            )
        has_bus = StudentTransport.objects.filter(student=kid, is_active=True).exists()
        for department in FAMILY_DEPARTMENTS:
            if department == Department.TRANSPORT and not has_bus:
                continue
            if not Membership.objects.filter(department=department, is_active=True).exists():
                continue
            contacts.append(
                {
                    "kind": "department",
                    "department": department,
                    "name": Department(department).label,
                    "initials": initials(Department(department).label),
                    "subtitle": "Office team",
                    "student": {"id": str(kid.id), "name": kid.full_name, "first_name": kid.first_name},
                }
            )
    return contacts


def staff_may_message_student_family(request, student: Student) -> bool:
    if request.roles & {Role.PRINCIPAL, Role.ADMIN}:
        return True
    if Role.TEACHER in request.roles and student.class_group_id in teacher_class_ids(request.user):
        return True
    return False


def family_may_message_teacher(request, teacher_id, student: Student) -> bool:
    contacts = family_contacts(request)
    return any(c["kind"] == "user" and c["user_id"] == str(teacher_id) and c["student"]["id"] == str(student.id) for c in contacts)


def guardian_users(student: Student) -> list:
    return [
        link.user
        for link in StudentGuardian.objects.filter(student=student).select_related("user")
        if link.user.is_active
    ]
