"""HR records about one staff member follow that staff member: with ``self`` scope a member sees their own
attendance days, leave requests, salary structure and payslips. Everything else is school scope only."""

from __future__ import annotations

from django.db.models import Q

from eduflow.authz.catalog import DataScope
from eduflow.authz.scopes import ScopedResource
from eduflow.people.scoping import staff_self_rule

from .models import (
    Candidate,
    JobOpening,
    LeaveRequest,
    LeaveType,
    PayrollRun,
    Payslip,
    SalaryStructure,
    StaffAttendance,
)

staff_days: ScopedResource[StaffAttendance] = ScopedResource("staff_attendance", StaffAttendance)
leave_types: ScopedResource[LeaveType] = ScopedResource("leave_type", LeaveType)
leave_requests: ScopedResource[LeaveRequest] = ScopedResource("leave_request", LeaveRequest)
salaries: ScopedResource[SalaryStructure] = ScopedResource("salary_structure", SalaryStructure)
runs: ScopedResource[PayrollRun] = ScopedResource("payroll_run", PayrollRun)
payslips: ScopedResource[Payslip] = ScopedResource("payslip", Payslip)
openings: ScopedResource[JobOpening] = ScopedResource("job_opening", JobOpening)
candidates: ScopedResource[Candidate] = ScopedResource("candidate", Candidate)

for _resource in (staff_days, leave_requests, salaries, payslips):
    staff_self_rule(_resource)

# Leave types are the school's policy, not personal data: anyone who may request leave sees all of them.
leave_types.rule(DataScope.SELF)(lambda actor: Q(pk__isnull=False))
