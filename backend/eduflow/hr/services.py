"""HR writes and selectors. Transactional and audited (``hr.*``)."""

from __future__ import annotations

import datetime
from decimal import Decimal
from typing import Any

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from eduflow.academics.models import AcademicYear, Department
from eduflow.authz.grants import Actor
from eduflow.core.api import Conflict
from eduflow.documents import files
from eduflow.notifications import services as notifications
from eduflow.people.models import StaffProfile, StaffStatus
from eduflow.tenancy import clock, domain

from .models import (
    Candidate,
    CandidateStage,
    HrSettings,
    JobOpening,
    LeaveRequest,
    LeaveStatus,
    LeaveType,
    PayrollRun,
    Payslip,
    RunStatus,
    SalaryStructure,
    StaffAttendance,
    StaffDayStatus,
)

DEFAULT_LATE_AFTER = datetime.time(8, 0)  # prototype: "Checked in after 08:00"


def own_staff(actor: Actor) -> StaffProfile:
    staff = StaffProfile.objects.filter(school_id=actor.school.pk, membership=actor.membership).first()
    if staff is None:
        raise PermissionDenied("Only staff members can do this.")
    return staff


# ------------------------------------------------------------------------------------------------ settings
def late_after(school: Any) -> datetime.time:
    row = HrSettings.objects.filter(school_id=school.pk).first()
    return row.late_after if row else DEFAULT_LATE_AFTER


@transaction.atomic
def set_late_after(actor: Actor, value: datetime.time) -> datetime.time:
    row, _ = HrSettings.objects.update_or_create(school_id=actor.school.pk, defaults={"late_after": value})
    domain.record("hr.settings.updated", row, late_after=value.isoformat())
    return row.late_after


# ------------------------------------------------------------------------------------------------ attendance
@transaction.atomic
def check_in(actor: Actor) -> StaffAttendance:
    staff = own_staff(actor)
    now = clock.now(actor.school)
    day = StaffAttendance.objects.select_for_update().filter(staff=staff, date=now.date()).first()
    if day is not None and day.check_in is not None:
        raise Conflict("You have already checked in today.")
    if day is not None and day.status == StaffDayStatus.LEAVE:
        raise Conflict("You are on approved leave today.")
    status = StaffDayStatus.LATE if now.time() > late_after(actor.school) else StaffDayStatus.PRESENT
    day = day or StaffAttendance(school=actor.school, staff=staff, date=now.date())
    day.status, day.check_in, day.recorded_by = status, now.time().replace(microsecond=0), actor.membership
    day.save()
    domain.record("hr.staff_attendance.checked_in", day, status=status)
    return day


@transaction.atomic
def check_out(actor: Actor) -> StaffAttendance:
    staff = own_staff(actor)
    now = clock.now(actor.school)
    day = StaffAttendance.objects.select_for_update().filter(staff=staff, date=now.date()).first()
    if day is None or day.check_in is None:
        raise Conflict("Check in first.")
    if day.check_out is not None:
        raise Conflict("You have already checked out today.")
    day.check_out = now.time().replace(microsecond=0)
    day.save(update_fields=["check_out", "updated_at"])
    domain.record("hr.staff_attendance.checked_out", day)
    return day


@transaction.atomic
def record_day(
    actor: Actor,
    *,
    staff_id: Any,
    date: datetime.date,
    status: str,
    check_in: datetime.time | None = None,
    check_out: datetime.time | None = None,
    note: str = "",
) -> StaffAttendance:
    """The office records or corrects a day (school-wide ``staff_attendance.manage``)."""
    staff = domain.resolve(StaffProfile, actor.school, staff_id, "staff_id", label="staff member")
    if date > clock.today(actor.school):
        raise ValidationError({"date": ["A day in the future cannot be recorded."]})
    if status in (StaffDayStatus.PRESENT, StaffDayStatus.LATE) and check_in is None:
        raise ValidationError({"check_in": ["Give the check-in time."]})
    if check_out is not None and (check_in is None or check_out < check_in):
        raise ValidationError({"check_out": ["The check-out is before the check-in."]})
    day, created = StaffAttendance.objects.update_or_create(
        school_id=actor.school.pk,
        staff=staff,
        date=date,
        defaults={
            "status": status,
            "check_in": check_in,
            "check_out": check_out,
            "note": note,
            "recorded_by": actor.membership,
        },
    )
    domain.record("hr.staff_attendance.recorded", day, status=status, created=created)
    return day


# ------------------------------------------------------------------------------------------------ leave
def _year_of(school: Any, day: datetime.date) -> AcademicYear | None:
    return AcademicYear.objects.filter(school_id=school.pk, start_date__lte=day, end_date__gte=day).first()


def balance(
    staff: StaffProfile, leave_type: LeaveType, on: datetime.date, *, pending: bool = False
) -> Decimal:
    """Days left of a type in the academic year of ``on``; with ``pending``, pending requests count as used
    (so a new request cannot overbook)."""
    year = _year_of(staff.school, on)
    states = [LeaveStatus.APPROVED, LeaveStatus.PENDING] if pending else [LeaveStatus.APPROVED]
    taken = LeaveRequest.objects.filter(staff=staff, leave_type=leave_type, status__in=states)
    if year is not None:
        taken = taken.filter(start_date__gte=year.start_date, start_date__lte=year.end_date)
    used = taken.aggregate(t=Sum("days"))["t"] or Decimal(0)
    return leave_type.days_per_year - used


@transaction.atomic
def request_leave(
    actor: Actor,
    *,
    leave_type_id: Any,
    start_date: datetime.date,
    end_date: datetime.date,
    reason: str,
    days: Decimal | None = None,
) -> LeaveRequest:
    staff = own_staff(actor)
    leave_type = domain.resolve(LeaveType, actor.school, leave_type_id, "leave_type_id", label="leave type")
    if not leave_type.is_active:
        raise ValidationError({"leave_type_id": ["This leave type is no longer offered."]})
    if end_date < start_date:
        raise ValidationError({"end_date": ["The leave ends before it starts."]})
    calendar_days = Decimal((end_date - start_date).days + 1)
    days = days if days is not None else calendar_days
    if days <= 0 or days > calendar_days:
        raise ValidationError({"days": [f"Between 0.5 and {calendar_days}."]})
    overlapping = LeaveRequest.objects.filter(
        staff=staff,
        status__in=[LeaveStatus.PENDING, LeaveStatus.APPROVED],
        start_date__lte=end_date,
        end_date__gte=start_date,
    )
    if overlapping.exists():
        raise Conflict("You already have leave requested for some of these days.")
    left = balance(staff, leave_type, start_date, pending=True)
    if days > left:
        raise ValidationError({"days": [f"Only {left} day(s) left, counting pending requests."]})
    leave = LeaveRequest.objects.create(
        school=actor.school,
        staff=staff,
        leave_type=leave_type,
        start_date=start_date,
        end_date=end_date,
        days=days,
        reason=reason,
    )
    domain.record("hr.leave.requested", leave, type=leave_type.name, days=str(days))
    return leave


@transaction.atomic
def decide_leave(actor: Actor, leave: LeaveRequest, decision: str, note: str = "") -> LeaveRequest:
    leave = (
        LeaveRequest.objects.select_for_update(of=("self",))
        .select_related("staff", "leave_type")
        .get(pk=leave.pk)
    )
    if leave.status != LeaveStatus.PENDING:
        raise Conflict("This leave request has already been decided.")
    if decision == "approve" and leave.days > balance(leave.staff, leave.leave_type, leave.start_date):
        raise Conflict("Approving this would exceed the leave balance.")
    leave.status = LeaveStatus.APPROVED if decision == "approve" else LeaveStatus.DECLINED
    leave.decided_by, leave.decided_at, leave.decision_note = actor.membership, timezone.now(), note
    leave.save()
    if leave.status == LeaveStatus.APPROVED:
        day = leave.start_date
        while day <= leave.end_date:
            StaffAttendance.objects.update_or_create(
                school_id=leave.school_id,
                staff=leave.staff,
                date=day,
                defaults={
                    "status": StaffDayStatus.LEAVE,
                    "check_in": None,
                    "check_out": None,
                    "note": "Leave",
                },
            )
            day += datetime.timedelta(days=1)
    domain.record("hr.leave.decided", leave, decision=leave.status)
    notifications.notify(
        actor.school,
        [leave.staff.membership],
        kind="approval",
        title=f"Leave {leave.status}",
        body=note,
        link=("leave_request", leave.pk),
    )
    return leave


@transaction.atomic
def cancel_leave(actor: Actor, leave: LeaveRequest) -> LeaveRequest:
    if leave.staff.membership_id != actor.membership.pk:
        raise PermissionDenied("Only the requester can cancel a leave request.")
    leave = LeaveRequest.objects.select_for_update().get(pk=leave.pk)
    if leave.status != LeaveStatus.PENDING:
        raise Conflict("Only a pending leave request can be cancelled.")
    leave.status = LeaveStatus.CANCELLED
    leave.save(update_fields=["status"])
    domain.record("hr.leave.cancelled", leave)
    return leave


# ------------------------------------------------------------------------------------------------ payroll
@transaction.atomic
def set_salary(actor: Actor, staff: StaffProfile, **amounts: Any) -> SalaryStructure:
    salary, created = SalaryStructure.objects.update_or_create(
        school_id=actor.school.pk, staff=staff, defaults=amounts
    )
    domain.record("hr.salary.set", salary, staff=str(staff.pk), created=created, fields=sorted(amounts))
    return salary


@transaction.atomic
def create_run(actor: Actor, *, month: datetime.date) -> PayrollRun:
    run = PayrollRun(school=actor.school, month=month.replace(day=1))
    domain.save(run, conflict="A payroll run for this month already exists.")
    domain.record("hr.payroll_run.created", run, month=run.month.isoformat()[:7])
    return run


@transaction.atomic
def process_run(actor: Actor, run: PayrollRun) -> PayrollRun:
    """Generate the payslips from the entered salary structures of active staff and freeze them."""
    run = PayrollRun.objects.select_for_update().get(pk=run.pk)
    if run.status != RunStatus.DRAFT:
        raise Conflict(f"This payroll run is already {run.status}.")
    structures = SalaryStructure.objects.filter(
        school_id=run.school_id, staff__status=StaffStatus.ACTIVE
    ).select_related("staff")
    slips = []
    for s in structures:
        gross = s.basic + s.hra + s.allowances
        deductions = s.pf + s.esi + s.tds
        slips.append(
            Payslip(
                school_id=run.school_id,
                run=run,
                staff=s.staff,
                basic=s.basic,
                hra=s.hra,
                allowances=s.allowances,
                pf=s.pf,
                esi=s.esi,
                tds=s.tds,
                gross=gross,
                deductions=deductions,
                net=gross - deductions,
            )
        )
    if not slips:
        raise Conflict("No active staff member has a salary structure.")
    Payslip.objects.bulk_create(slips)
    run.status, run.processed_at, run.processed_by = RunStatus.PROCESSED, timezone.now(), actor.membership
    run.save()
    domain.record("hr.payroll_run.processed", run, payslips=len(slips), net=str(sum(p.net for p in slips)))
    return run


@transaction.atomic
def mark_paid(actor: Actor, run: PayrollRun) -> PayrollRun:
    run = PayrollRun.objects.select_for_update().get(pk=run.pk)
    if run.status != RunStatus.PROCESSED:
        raise Conflict("Only a processed payroll run can be marked paid.")
    run.status, run.paid_at = RunStatus.PAID, timezone.now()
    run.save()
    domain.record("hr.payroll_run.paid", run)
    members = [p.staff.membership for p in run.payslips.select_related("staff__membership")]
    notifications.notify(
        actor.school,
        members,
        kind="approval",
        title=f"Payslip for {run.month:%B %Y}",
        link=("payroll_run", run.pk),
    )
    return run


def run_totals(run: PayrollRun) -> dict[str, Any]:
    totals = run.payslips.aggregate(gross=Sum("gross"), deductions=Sum("deductions"), net=Sum("net"))
    return {k: v or Decimal(0) for k, v in totals.items()} | {"staff_count": run.payslips.count()}


# ------------------------------------------------------------------------------------------------ recruitment
@transaction.atomic
def create_opening(actor: Actor, **data: Any) -> JobOpening:
    dept_id = data.pop("department_id", None)
    data["department"] = (
        domain.resolve(Department, actor.school, dept_id, "department_id") if dept_id else None
    )
    opening = JobOpening.objects.create(school=actor.school, **data)
    domain.record("hr.job_opening.created", opening, title=opening.title)
    return opening


@transaction.atomic
def update_opening(actor: Actor, opening: JobOpening, **data: Any) -> JobOpening:
    if "department_id" in data:
        dept_id = data.pop("department_id")
        data["department"] = (
            domain.resolve(Department, actor.school, dept_id, "department_id") if dept_id else None
        )
    changed = domain.apply_changes(opening, data)
    if changed:
        opening.save()
        domain.record("hr.job_opening.updated", opening, fields=changed)
    return opening


@transaction.atomic
def add_candidate(actor: Actor, *, opening_id: Any, resume: Any = None, **data: Any) -> Candidate:
    opening = domain.resolve(JobOpening, actor.school, opening_id, "opening_id", label="job opening")
    if opening.status != "open":
        raise ValidationError({"opening_id": ["This opening is closed."]})
    stored = files.store(actor.school.pk, resume, actor.membership) if resume is not None else None
    candidate = Candidate.objects.create(school=actor.school, opening=opening, resume=stored, **data)
    domain.record("hr.candidate.added", candidate, opening=str(opening.pk))
    return candidate


TERMINAL_STAGES = {CandidateStage.HIRED, CandidateStage.REJECTED}


@transaction.atomic
def update_candidate(actor: Actor, candidate: Candidate, **data: Any) -> Candidate:
    if candidate.stage in TERMINAL_STAGES and "stage" in data and data["stage"] != candidate.stage:
        raise Conflict(f"This candidate is {candidate.stage}.")
    changed = domain.apply_changes(candidate, data)
    if changed:
        candidate.save()
        domain.record("hr.candidate.updated", candidate, fields=changed, stage=candidate.stage)
    return candidate


# ------------------------------------------------------------------------------------------------ monitoring
def late_counts(school: Any, since: datetime.date, until: datetime.date) -> dict[Any, int]:
    rows = StaffAttendance.objects.filter(
        school_id=school.pk, status=StaffDayStatus.LATE, date__gte=since, date__lte=until
    ).values_list("staff_id", flat=True)
    counts: dict[Any, int] = {}
    for staff_id in rows:
        counts[staff_id] = counts.get(staff_id, 0) + 1
    return counts


def punctuality(school: Any, since: datetime.date, until: datetime.date) -> dict[Any, float]:
    """Per staff member: present days / (present + late) days (prototype scorecard input)."""
    stats: dict[Any, list[int]] = {}
    rows = StaffAttendance.objects.filter(
        school_id=school.pk,
        status__in=[StaffDayStatus.PRESENT, StaffDayStatus.LATE],
        date__gte=since,
        date__lte=until,
    ).values_list("staff_id", "status")
    for staff_id, status in rows:
        entry = stats.setdefault(staff_id, [0, 0])
        entry[1] += 1
        if status == StaffDayStatus.PRESENT:
            entry[0] += 1
    return {k: v[0] / v[1] for k, v in stats.items()}


def absent_without_leave(school: Any, day: datetime.date) -> list[StaffProfile]:
    """Active staff with no check-in and no approved leave on the day ("Absent without leave")."""
    accounted = StaffAttendance.objects.filter(
        school_id=school.pk,
        date=day,
        status__in=[StaffDayStatus.PRESENT, StaffDayStatus.LATE, StaffDayStatus.LEAVE],
    ).values("staff_id")
    on_leave = LeaveRequest.objects.filter(
        school_id=school.pk, status=LeaveStatus.APPROVED, start_date__lte=day, end_date__gte=day
    ).values("staff_id")
    return list(
        StaffProfile.objects.filter(school_id=school.pk, status=StaffStatus.ACTIVE)
        .exclude(pk__in=accounted)
        .exclude(pk__in=on_leave)
        .select_related("membership__user")
    )
