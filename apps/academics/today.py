"""A child's school day at a glance: check-in, the period ribbon, what's on now, and home time."""

from datetime import datetime, timedelta

from apps.core.utils import school_now, school_tz

from .models import Student, TimetableSlot

# A gap of this many minutes between periods is shown as a break on the ribbon.
BREAK_MINUTES = 10


def check_in(student: Student, today) -> dict | None:
    """When the child arrived: off the morning bus at school, or when the register marked them in."""
    from apps.attendance.models import AttendanceException, AttendanceSession
    from apps.transport.models import BoardingEvent, Direction, Trip

    event = (
        BoardingEvent.objects.filter(
            student=student,
            kind=BoardingEvent.Kind.DROPPED,
            trip__service_date=today,
            trip__direction=Direction.PICKUP,
        )
        .exclude(trip__status=Trip.Status.CANCELLED)
        .order_by("at")
        .first()
    )
    session = AttendanceSession.objects.filter(class_group=student.class_group, date=today).first()
    exception = AttendanceException.objects.filter(session=session, student=student).first() if session else None
    if exception and exception.status in ("absent", "excused"):
        return {"status": exception.status, "at": None, "source": "register", "note": exception.note}
    if event:
        return {"status": exception.status if exception else "present", "at": event.at.isoformat(), "source": "bus", "note": ""}
    if session:
        return {
            "status": exception.status if exception else "present",
            "at": session.marked_at.isoformat() if session.marked_at else None,
            "source": "register",
            "note": exception.note if exception else "",
        }
    return None


def day_ribbon(student: Student, day) -> dict:
    """Periods for `day` with done / now / todo states, and breaks where the timetable has a gap."""
    tz = school_tz(student.school)
    now = school_now(student.school)
    is_today = now.date() == day
    slots = list(
        TimetableSlot.objects.filter(class_group=student.class_group, weekday=day.weekday())
        .select_related("subject", "teacher")
        .order_by("period")
    )
    cells, current, nxt = [], None, None
    prev_end = None
    for slot in slots:
        start = datetime.combine(day, slot.starts_at, tzinfo=tz)
        end = datetime.combine(day, slot.ends_at, tzinfo=tz)
        if prev_end is not None and (start - prev_end) >= timedelta(minutes=BREAK_MINUTES):
            cells.append({"kind": "break", "starts_at": prev_end.strftime("%H:%M"), "ends_at": start.strftime("%H:%M")})
        if not is_today:
            state = "todo" if day > now.date() else "done"
        elif now >= end:
            state = "done"
        elif now >= start:
            state = "now"
        else:
            state = "todo"
        item = {
            "kind": "period",
            "period": slot.period,
            "state": state,
            "subject": slot.subject.name,
            "short": slot.subject.short_name,
            "code": slot.subject.code,
            "color": slot.subject.color,
            "teacher": slot.teacher.full_name if slot.teacher else None,
            "room": slot.room,
            "note": slot.note,
            "starts_at": slot.starts_at.strftime("%H:%M"),
            "ends_at": slot.ends_at.strftime("%H:%M"),
        }
        cells.append(item)
        if state == "now":
            current = item
        elif state == "todo" and nxt is None:
            nxt = item
        prev_end = end
    return {
        "date": day.isoformat(),
        "cells": cells,
        "current": current,
        "next": nxt,
        "starts_at": slots[0].starts_at.strftime("%H:%M") if slots else None,
        "ends_at": slots[-1].ends_at.strftime("%H:%M") if slots else None,
        "periods": len(slots),
    }


def home_time(student: Student, today) -> dict | None:
    """The drop run's time at the child's stop: live ETA if the bus is out, else the timetable."""
    from apps.transport.models import Direction, StudentTransport, Trip
    from apps.transport.services import live_state

    assignment = (
        StudentTransport.objects.filter(student=student, is_active=True)
        .select_related("route__vehicle", "drop_stop")
        .first()
    )
    if assignment is None:
        return None
    route, stop = assignment.route, assignment.drop_stop
    tz = school_tz(student.school)
    scheduled = datetime.combine(today, route.drop_start, tzinfo=tz) + timedelta(minutes=stop.drop_offset_min)
    trip = Trip.objects.filter(route=route, service_date=today, direction=Direction.DROP).first()
    eta = scheduled
    delay = 0
    if trip is not None and trip.status == Trip.Status.ACTIVE:
        live = live_state(trip)
        mine = next((s for s in live["stops"] if s["id"] == str(stop.id)), None)
        if mine and mine["eta_seconds"] is not None:
            eta = school_now(student.school) + timedelta(seconds=mine["eta_seconds"])
        delay = live.get("delay_minutes") or 0
    return {
        "bus": route.vehicle.label if route.vehicle else route.name,
        "route": route.name,
        "stop": stop.name,
        "scheduled_at": scheduled.isoformat(),
        "eta_at": eta.isoformat(),
        "delay_minutes": delay,
        "trip_status": trip.status if trip else None,
    }
