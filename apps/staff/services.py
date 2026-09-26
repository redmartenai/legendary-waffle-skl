"""A teacher's day and year: periods, covers, free time, and leave balances."""

from datetime import date, datetime, timedelta
from decimal import Decimal

from apps.academics.models import AcademicYear, ClassGroup, TimetableSlot
from apps.core.utils import school_now, school_tz

from .models import StaffLeave, Substitution

BREAK_MINUTES = 10
LUNCH_MINUTES = 30
DEFAULT_ALLOWANCE = {"casual": 12, "sick": 8, "earned": 15}


def school_periods(weekday: int) -> list[tuple[int, str, str]]:
    """The bell schedule for a weekday: (period, "HH:MM", "HH:MM"), taken from the timetable itself."""
    seen: dict = {}
    for period, starts, ends in TimetableSlot.objects.filter(weekday=weekday).values_list("period", "starts_at", "ends_at"):
        seen.setdefault(period, (starts, ends))
    return [(p, s.strftime("%H:%M"), e.strftime("%H:%M")) for p, (s, e) in sorted(seen.items())]


def _cover_payload(sub: Substitution) -> dict:
    return {
        "id": str(sub.id),
        "for": sub.absent_teacher.full_name if sub.absent_teacher else None,
        "for_first_name": sub.absent_teacher.full_name.split()[0] if sub.absent_teacher else None,
        "reason": sub.reason,
        "assigned_by": sub.assigned_by.full_name if sub.assigned_by else None,
        "assigned_at": sub.assigned_at.isoformat(),
        "note": sub.handover_note,
        "note_at": sub.handover_at.isoformat() if sub.handover_at else None,
    }


def teacher_day(user, school, day: date) -> dict:
    """Every period of the school day for this teacher: their class, a cover, covered by someone else, or free."""
    tz = school_tz(school)
    now = school_now(school)
    weekday = day.weekday()
    bells = school_periods(weekday) if weekday < 6 else []
    mine = {
        s.period: s
        for s in TimetableSlot.objects.filter(teacher=user, weekday=weekday).select_related("subject", "class_group", "class_group__class_teacher")
    }
    covers = {
        s.slot.period: s
        for s in Substitution.objects.filter(teacher=user, date=day).select_related("slot__subject", "slot__class_group", "absent_teacher", "assigned_by")
    }
    covered_away = {
        s.slot_id: s for s in Substitution.objects.filter(slot__in=list(mine.values()), date=day).exclude(teacher=user).select_related("teacher")
    }
    cells, current, nxt = [], None, None
    prev_end = None
    counts = {"classes": 0, "covers": 0, "free": 0}
    for period, starts, ends in bells:
        start = datetime.combine(day, datetime.strptime(starts, "%H:%M").time(), tzinfo=tz)
        end = datetime.combine(day, datetime.strptime(ends, "%H:%M").time(), tzinfo=tz)
        if prev_end is not None and (start - prev_end) >= timedelta(minutes=BREAK_MINUTES):
            gap = (start - prev_end).seconds // 60
            cells.append({"kind": "lunch" if gap >= LUNCH_MINUTES else "break", "starts_at": prev_end.strftime("%H:%M"), "ends_at": starts})
        prev_end = end
        if now.date() != day:
            state = "done" if day < now.date() else "todo"
        else:
            state = "done" if now >= end else "now" if now >= start else "todo"
        base = {"period": period, "starts_at": starts, "ends_at": ends, "state": state}
        if period in covers:
            sub = covers[period]
            slot = sub.slot
            item = {
                **base,
                "kind": "cover",
                "class": {"id": str(slot.class_group_id), "short_label": slot.class_group.short_label},
                "subject": slot.subject.name,
                "room": slot.room,
                "cover": _cover_payload(sub),
            }
            counts["covers"] += 1
        elif period in mine and mine[period].id in covered_away:
            slot = mine[period]
            item = {
                **base,
                "kind": "covered",
                "class": {"id": str(slot.class_group_id), "short_label": slot.class_group.short_label},
                "subject": slot.subject.name,
                "room": slot.room,
                "covered_by": covered_away[slot.id].teacher.full_name,
            }
        elif period in mine:
            slot = mine[period]
            item = {
                **base,
                "kind": "class",
                "class": {"id": str(slot.class_group_id), "short_label": slot.class_group.short_label},
                "subject": slot.subject.name,
                "room": slot.room,
                "is_my_class": slot.class_group.class_teacher_id == user.id,
                "note": slot.note,
            }
            counts["classes"] += 1
        else:
            item = {**base, "kind": "free"}
            counts["free"] += 1
        cells.append(item)
        if state == "now":
            current = item
        elif state == "todo" and nxt is None:
            nxt = item
    return {
        "date": day.isoformat(),
        "weekday": weekday,
        "cells": cells,
        "counts": counts,
        "current": current,
        "next": nxt,
        "starts_at": bells[0][1] if bells else None,
        "ends_at": bells[-1][2] if bells else None,
    }


def next_school_day(day: date) -> date:
    nxt = day + timedelta(days=1)
    return nxt + timedelta(days=1) if nxt.weekday() == 6 else nxt


def working_days(start: date, end: date, half_day: bool = False) -> Decimal:
    """Monday–Saturday between two dates (inclusive); a half day counts 0.5."""
    if half_day:
        return Decimal("0.5")
    n = sum(1 for i in range((end - start).days + 1) if (start + timedelta(days=i)).weekday() != 6)
    return Decimal(n)


def leave_year() -> AcademicYear | None:
    return AcademicYear.objects.filter(is_current=True).first()


def leave_balances(user, school) -> list[dict]:
    allowance = {**DEFAULT_ALLOWANCE, **((school.settings or {}).get("staff_leave") or {})}
    year = leave_year()
    requests = StaffLeave.objects.filter(user=user)
    if year:
        requests = requests.filter(from_date__gte=year.starts_on, from_date__lte=year.ends_on)
    used: dict = {k: Decimal("0") for k in allowance}
    pending: dict = {k: Decimal("0") for k in allowance}
    for r in requests:
        if r.status == StaffLeave.Status.APPROVED:
            used[r.kind] = used.get(r.kind, Decimal("0")) + r.days
        elif r.status == StaffLeave.Status.PENDING:
            pending[r.kind] = pending.get(r.kind, Decimal("0")) + r.days
    return [
        {
            "kind": kind,
            "allowed": allowance[kind],
            "used": float(used[kind]),
            "pending": float(pending[kind]),
            "left": float(Decimal(allowance[kind]) - used[kind]),
        }
        for kind in ("casual", "sick", "earned")
    ]


def periods_by_weekday(user) -> list[int]:
    counts = [0] * 7
    for weekday in TimetableSlot.objects.filter(teacher=user).values_list("weekday", flat=True):
        counts[weekday] += 1
    return counts


def taught_classes(user) -> list[ClassGroup]:
    from apps.academics.access import teacher_class_ids

    return list(
        ClassGroup.objects.filter(id__in=teacher_class_ids(user)).select_related("class_teacher").order_by("grade", "section")
    )
