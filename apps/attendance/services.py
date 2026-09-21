import calendar
from datetime import date

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from apps.academics.access import guardians_of
from apps.academics.models import AcademicYear, ClassGroup, Student
from apps.notifications.models import Category, Priority
from apps.notifications.services import notify

from .models import AttendanceException, AttendanceSession, AttendanceStatus

ALERT_STATUSES = {AttendanceStatus.ABSENT, AttendanceStatus.LATE}


def mark_class_attendance(class_group: ClassGroup, day: date, entries: list[dict], marked_by, client_id: str = "") -> dict:
    """Save a class's attendance. ``entries`` lists only students who were NOT simply present.

    Re-submitting (a correction, or a retried request) replaces the day's exceptions and never
    re-sends an alert the family already received for the same status.
    """
    roster = {str(s.id): s for s in Student.objects.filter(class_group=class_group, is_active=True)}
    cleaned: dict[str, str] = {}
    for entry in entries:
        student_id, status = str(entry.get("student_id")), entry.get("status")
        if student_id not in roster:
            raise ValidationError({"entries": f"Student {student_id} isn't in {class_group.label}."})
        if status not in AttendanceStatus.values:
            raise ValidationError({"entries": f"Unknown status {status!r}."})
        cleaned[student_id] = status

    with transaction.atomic():
        session, _ = AttendanceSession.objects.update_or_create(
            class_group=class_group,
            date=day,
            defaults={"marked_by": marked_by, "marked_at": timezone.now(), "client_id": client_id or ""},
        )
        existing = {str(e.student_id): e for e in AttendanceException.objects.filter(session=session)}
        for student_id, exception in existing.items():
            if student_id not in cleaned:
                exception.delete()
        for student_id, status in cleaned.items():
            current = existing.get(student_id)
            if current is None:
                AttendanceException.objects.create(session=session, student=roster[student_id], status=status)
            elif current.status != status:
                current.status = status
                current.save(update_fields=["status", "updated_at"])

        alerted = [roster[sid] for sid, status in cleaned.items() if status in ALERT_STATUSES]
        for student in alerted:
            status = cleaned[str(student.id)]
            label = "absent" if status == AttendanceStatus.ABSENT else "late"
            notify(
                list(guardians_of([student]).keys()),
                school=class_group.school,
                category=Category.ATTENDANCE,
                title=f"{student.first_name} was marked {label} today",
                body=f"{class_group.label} · {day.strftime('%d %b')}. Message the class teacher if this isn't right.",
                data={"student_id": str(student.id), "date": day.isoformat(), "type": f"attendance_{status}"},
                dedupe_key=f"attendance:{student.id}:{day.isoformat()}:{status}",
                priority=Priority.HIGH if status == AttendanceStatus.ABSENT else Priority.NORMAL,
            )

    counts = {s: sum(1 for v in cleaned.values() if v == s) for s in AttendanceStatus.values}
    return {
        "date": day.isoformat(),
        "total": len(roster),
        "present": len(roster) - counts[AttendanceStatus.ABSENT] - counts[AttendanceStatus.EXCUSED],
        **counts,
    }


def student_month(student: Student, year: int, month: int, today: date) -> dict:
    first = date(year, month, 1)
    last = date(year, month, calendar.monthrange(year, month)[1])
    sessions = {
        s.date: s.id
        for s in AttendanceSession.objects.filter(class_group=student.class_group, date__range=(first, last))
    }
    exceptions = {
        e.session_id: e.status
        for e in AttendanceException.objects.filter(student=student, session_id__in=sessions.values())
    }
    days = []
    for offset in range((last - first).days + 1):
        day = date.fromordinal(first.toordinal() + offset)
        if day in sessions:
            status = exceptions.get(sessions[day], "present")
        elif day > today:
            status = "upcoming"
        elif day.weekday() == 6:
            status = "holiday"
        else:
            status = "not_marked"
        days.append({"date": day.isoformat(), "status": status})
    marked = [d for d in days if d["status"] in {"present", "absent", "late", "half_day", "excused"}]
    present = sum(1 for d in marked if d["status"] in {"present", "late", "half_day"})
    return {
        "month": f"{year:04d}-{month:02d}",
        "days": days,
        "summary": {
            "school_days": len(marked),
            "present": present,
            "absent": sum(1 for d in marked if d["status"] == "absent"),
            "late": sum(1 for d in marked if d["status"] == "late"),
            "percent": round(100 * present / len(marked), 1) if marked else None,
        },
    }


def student_term_percent(student: Student) -> float | None:
    year = AcademicYear.objects.filter(is_current=True).first()
    sessions = AttendanceSession.objects.filter(class_group=student.class_group)
    if year:
        sessions = sessions.filter(date__gte=year.starts_on)
    total = sessions.count()
    if not total:
        return None
    absent = AttendanceException.objects.filter(
        student=student, session__in=sessions, status__in=[AttendanceStatus.ABSENT, AttendanceStatus.EXCUSED]
    ).count()
    return round(100 * (total - absent) / total, 1)


def student_today(student: Student, today: date) -> str:
    session = AttendanceSession.objects.filter(class_group=student.class_group, date=today).first()
    if session is None:
        return "not_marked"
    exception = AttendanceException.objects.filter(session=session, student=student).first()
    return exception.status if exception else "present"
