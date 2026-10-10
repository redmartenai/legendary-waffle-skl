"""Human resources (prototype ``StaffDay``, ``Salary``, ``PayrollRun``, leave approvals; screen documentation
"Staff attendance", "Leave", "Payroll", "Payslips", "Recruitment"; monitoring rules "Absent without leave"
and "Frequent late arrivals").

Staff attendance
    One row per staff member per day: present, late, absent or on leave, with check-in / check-out times.
    A self check-in after the school's ``late_after`` time (prototype: 08:00) is ``late``. Approved leave
    writes ``leave`` days.

Leave
    Leave types are the school's own (name and days per year): EduFlow does not invent a leave policy.
    A request is decided in the approvals queue (``leave``). The balance of a type is its yearly days less
    the approved days in that academic year.

Payroll
    The salary structure holds the amounts the school enters: basic, HRA, allowances and the PF, ESI and TDS
    deductions. **EduFlow does not calculate statutory deductions** (PF, ESI, TDS rules differ by
    registration, state and regime); it applies the entered amounts. A payroll run for a month moves
    draft -> processed (payslips are generated and frozen) -> paid.

Recruitment
    Job openings and candidates moving through stages. The stage list is provisional (no source defines it).
"""

from __future__ import annotations

from django.db import models
from django.db.models import F, Q

from eduflow.academics.models import Department
from eduflow.core.ids import uuid7
from eduflow.documents.models import StoredFile
from eduflow.people.models import StaffProfile
from eduflow.tenancy.models import Membership, TenantModel, TenantQuerySet


def _uniq(model: str) -> models.UniqueConstraint:
    return models.UniqueConstraint(fields=["id", "school"], name=f"hr_{model}_id_school_uniq")


class HrSettings(TenantModel):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    late_after = models.TimeField(help_text="A check-in after this school-local time is late.")

    objects = TenantQuerySet.as_manager()

    class Meta:
        db_table = "hr_settings"
        constraints = [models.UniqueConstraint(fields=["school"], name="hr_settings_school_uniq")]


# ------------------------------------------------------------------------------------------------ attendance
class StaffDayStatus(models.TextChoices):
    PRESENT = "present", "Present"
    LATE = "late", "Late"
    ABSENT = "absent", "Absent"
    LEAVE = "leave", "On leave"


class StaffAttendance(TenantModel):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    staff = models.ForeignKey(StaffProfile, on_delete=models.PROTECT, related_name="days")
    date = models.DateField()
    status = models.CharField(max_length=16, choices=StaffDayStatus.choices)
    check_in = models.TimeField(null=True, blank=True)
    check_out = models.TimeField(null=True, blank=True)
    note = models.CharField(max_length=300, blank=True)
    recorded_by = models.ForeignKey(Membership, on_delete=models.SET_NULL, null=True, related_name="+")
    updated_at = models.DateTimeField(auto_now=True)

    objects = TenantQuerySet.as_manager()

    class Meta:
        db_table = "hr_staff_attendance"
        constraints = [
            models.UniqueConstraint(fields=["staff", "date"], name="hr_staff_day_uniq"),
            models.CheckConstraint(
                condition=Q(status__in=StaffDayStatus.values), name="hr_staff_day_status_check"
            ),
            models.CheckConstraint(
                condition=Q(check_out__isnull=True)
                | Q(check_in__isnull=False) & Q(check_out__gte=F("check_in")),
                name="hr_staff_day_times_check",
            ),
            _uniq("staff_attendance"),
        ]
        indexes = [models.Index(fields=["school", "date"], name="hr_staff_day_date_idx")]


# ------------------------------------------------------------------------------------------------ leave
class LeaveType(TenantModel):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    name = models.CharField(max_length=60)
    days_per_year = models.DecimalField(max_digits=5, decimal_places=1)
    is_active = models.BooleanField(default=True)

    objects = TenantQuerySet.as_manager()

    class Meta:
        db_table = "hr_leave_type"
        constraints = [
            models.UniqueConstraint(fields=["school", "name"], name="hr_leave_type_name_uniq"),
            models.CheckConstraint(condition=Q(days_per_year__gte=0), name="hr_leave_type_days_check"),
            _uniq("leave_type"),
        ]


class LeaveStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    APPROVED = "approved", "Approved"
    DECLINED = "declined", "Declined"
    CANCELLED = "cancelled", "Cancelled"


class LeaveRequest(TenantModel):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    staff = models.ForeignKey(StaffProfile, on_delete=models.PROTECT, related_name="leave_requests")
    leave_type = models.ForeignKey(LeaveType, on_delete=models.PROTECT, related_name="+")
    start_date = models.DateField()
    end_date = models.DateField()
    days = models.DecimalField(max_digits=5, decimal_places=1)
    reason = models.CharField(max_length=500)
    status = models.CharField(max_length=16, choices=LeaveStatus.choices, default=LeaveStatus.PENDING)
    decided_by = models.ForeignKey(
        Membership, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_note = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = TenantQuerySet.as_manager()

    class Meta:
        db_table = "hr_leave_request"
        constraints = [
            models.CheckConstraint(condition=Q(end_date__gte=F("start_date")), name="hr_leave_dates_check"),
            models.CheckConstraint(condition=Q(days__gt=0), name="hr_leave_days_check"),
            _uniq("leave_request"),
        ]


# ------------------------------------------------------------------------------------------------ payroll
class SalaryStructure(TenantModel):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    staff = models.OneToOneField(StaffProfile, on_delete=models.PROTECT, related_name="salary")
    basic = models.DecimalField(max_digits=12, decimal_places=2)
    hra = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    allowances = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    pf = models.DecimalField(max_digits=12, decimal_places=2, default=0, help_text="Entered, not calculated.")
    esi = models.DecimalField(
        max_digits=12, decimal_places=2, default=0, help_text="Entered, not calculated."
    )
    tds = models.DecimalField(
        max_digits=12, decimal_places=2, default=0, help_text="Entered, not calculated."
    )
    bank_account_last4 = models.CharField(max_length=4, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = TenantQuerySet.as_manager()

    class Meta:
        db_table = "hr_salary_structure"
        constraints = [
            models.CheckConstraint(
                condition=Q(basic__gte=0)
                & Q(hra__gte=0)
                & Q(allowances__gte=0)
                & Q(pf__gte=0)
                & Q(esi__gte=0)
                & Q(tds__gte=0),
                name="hr_salary_non_negative_check",
            ),
            _uniq("salary_structure"),
        ]


class RunStatus(models.TextChoices):
    DRAFT = "draft", "Draft"
    PROCESSED = "processed", "Processed"
    PAID = "paid", "Paid"


class PayrollRun(TenantModel):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    month = models.DateField(help_text="The first day of the month paid.")
    status = models.CharField(max_length=16, choices=RunStatus.choices, default=RunStatus.DRAFT)
    processed_at = models.DateTimeField(null=True, blank=True)
    processed_by = models.ForeignKey(
        Membership, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    paid_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = TenantQuerySet.as_manager()

    class Meta:
        db_table = "hr_payroll_run"
        constraints = [
            models.UniqueConstraint(fields=["school", "month"], name="hr_payroll_month_uniq"),
            models.CheckConstraint(condition=Q(month__day=1), name="hr_payroll_month_check"),
            _uniq("payroll_run"),
        ]


class Payslip(TenantModel):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    run = models.ForeignKey(PayrollRun, on_delete=models.CASCADE, related_name="payslips")
    staff = models.ForeignKey(StaffProfile, on_delete=models.PROTECT, related_name="payslips")
    basic = models.DecimalField(max_digits=12, decimal_places=2)
    hra = models.DecimalField(max_digits=12, decimal_places=2)
    allowances = models.DecimalField(max_digits=12, decimal_places=2)
    pf = models.DecimalField(max_digits=12, decimal_places=2)
    esi = models.DecimalField(max_digits=12, decimal_places=2)
    tds = models.DecimalField(max_digits=12, decimal_places=2)
    gross = models.DecimalField(max_digits=12, decimal_places=2)
    deductions = models.DecimalField(max_digits=12, decimal_places=2)
    net = models.DecimalField(max_digits=12, decimal_places=2)

    objects = TenantQuerySet.as_manager()

    class Meta:
        db_table = "hr_payslip"
        constraints = [
            models.UniqueConstraint(fields=["run", "staff"], name="hr_payslip_uniq"),
            models.CheckConstraint(
                condition=Q(gross=F("basic") + F("hra") + F("allowances"))
                & Q(deductions=F("pf") + F("esi") + F("tds"))
                & Q(net=F("gross") - F("deductions")),
                name="hr_payslip_sums_check",
            ),
            _uniq("payslip"),
        ]


# ------------------------------------------------------------------------------------------------ recruitment
class OpeningStatus(models.TextChoices):
    OPEN = "open", "Open"
    CLOSED = "closed", "Closed"


class CandidateStage(models.TextChoices):
    APPLIED = "applied", "Applied"
    SCREENING = "screening", "Screening"
    INTERVIEW = "interview", "Interview"
    OFFER = "offer", "Offer"
    HIRED = "hired", "Hired"
    REJECTED = "rejected", "Rejected"


class JobOpening(TenantModel):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    title = models.CharField(max_length=150)
    department = models.ForeignKey(
        Department, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    positions = models.PositiveSmallIntegerField(default=1)
    description = models.TextField(max_length=5000, blank=True)
    status = models.CharField(max_length=16, choices=OpeningStatus.choices, default=OpeningStatus.OPEN)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = TenantQuerySet.as_manager()

    class Meta:
        db_table = "hr_job_opening"
        constraints = [
            models.CheckConstraint(condition=Q(positions__gte=1), name="hr_opening_positions_check"),
            _uniq("job_opening"),
        ]


class Candidate(TenantModel):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    opening = models.ForeignKey(JobOpening, on_delete=models.PROTECT, related_name="candidates")
    full_name = models.CharField(max_length=200)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=32, blank=True)
    stage = models.CharField(max_length=16, choices=CandidateStage.choices, default=CandidateStage.APPLIED)
    notes = models.CharField(max_length=2000, blank=True)
    resume = models.ForeignKey(StoredFile, on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = TenantQuerySet.as_manager()

    class Meta:
        db_table = "hr_candidate"
        constraints = [_uniq("candidate")]
