"""The permission catalogue, data scopes and the default role matrix (ADR-004).

See docs/security/authorization.md.

This file is the source of truth. The ``authz_permission`` table mirrors ``PERMISSIONS`` and is synced after
every ``migrate`` (and by ``manage.py sync_rbac``). ``SYSTEM_ROLES`` seeds the roles of each new school.

Adding a permission
-------------------
1. Add ``"<resource>.<action>": "description"`` to ``PERMISSIONS``.
2. Grant it in ``SYSTEM_ROLES`` to the roles that should have it by default, with a data scope.
3. Protect the endpoint: ``required_permissions = {"GET": "<resource>.<action>"}``.
4. Run ``manage.py sync_rbac`` (deployments run it after ``migrate``) so existing schools' system roles get
   the new default grants. It only ever adds grants; it never removes a school's customisations.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

from django.db import models

CODENAME = re.compile(r"^[a-z][a-z_]*\.[a-z][a-z_]*$")


class DataScope(models.TextChoices):
    """How much of a resource a permission covers. Scopes from several roles combine as a union.

    ``section`` is a homeroom class (ADR-007: the client's "class" is a section).
    """

    PLATFORM = "platform", "Platform-wide"
    SCHOOL = "school", "Whole school"
    CAMPUS = "campus", "Own campus"
    ACADEMIC_YEAR = "academic_year", "Academic year"
    DEPARTMENT = "department", "Own department"
    SECTION = "section", "Assigned classes/sections"
    ASSIGNED = "assigned", "Assigned students"
    CHILD = "child", "Own children"
    SELF = "self", "Own record"
    OWN = "own", "Records they created"


PERMISSIONS: Mapping[str, str] = {
    "school.read": "View the school profile",
    "school.update": "Edit the school profile",
    "user.read": "View members of the school",
    "user.create": "Add members to the school",
    "user.update": "Edit members",
    "user.disable": "Deactivate or reactivate members",
    "role.read": "View roles and their permissions",
    "role.create": "Create custom roles",
    "role.update": "Edit role permissions",
    "role.delete": "Delete custom roles",
    "role.assign": "Give or remove roles",
    "permission.read": "View the permission catalogue",
    "audit.read": "View the school's audit trail",
    "student.read": "View students",
    "student.create": "Admit students",
    "student.update": "Edit student records",
    "staff.read": "View staff records",
    "staff.update": "Edit staff records",
    "attendance.read": "View attendance",
    "attendance.create": "Take attendance",
    "attendance.update": "Correct attendance",
    "assessment.read": "View marks and results",
    "assessment.create": "Enter marks",
    "assessment.update": "Edit or moderate marks",
    "timetable.read": "View timetables",
    "report.read": "View reports",
    "fee.read": "View fees and payments",
    "fee.update": "Record fees and payments",
    "library.read": "View the library catalogue and loans",
    "library.manage": "Manage the library",
    "transport.read": "View routes and trips",
    "transport.manage": "Manage transport",
    "hostel.read": "View hostel allocations",
    "hostel.manage": "Manage hostels",
    # Phase 3: core school domain. "manage" covers create, update, archive and delete.
    "campus.read": "View campuses",
    "campus.manage": "Manage campuses",
    "academic_year.read": "View academic years",
    "academic_year.manage": "Manage academic years and their lifecycle",
    "department.read": "View departments",
    "department.manage": "Manage departments",
    "grade.read": "View grades (classes)",
    "grade.manage": "Manage grades (classes)",
    "section.read": "View sections",
    "section.manage": "Manage sections",
    "subject.read": "View subjects",
    "subject.manage": "Manage subjects",
    "staff.create": "Create staff and teacher profiles",
    "guardian.read": "View guardians and their links to students",
    "guardian.manage": "Manage guardians and their links to students",
    "enrollment.read": "View enrollments",
    "enrollment.manage": "Enroll, withdraw, complete and transfer students",
    "teacher_assignment.read": "View teacher assignments",
    "teacher_assignment.manage": "Assign teachers to sections and subjects",
    # Phase 4: invitations (onboarding and account linking).
    "invitation.read": "View invitations",
    "invitation.manage": "Invite staff, students and guardians; resend and revoke invitations",
    # Phase 5: academic engine. "timetable.read" (Phase 2) covers timetables, periods, slots and schedules.
    "term.read": "View academic terms",
    "term.manage": "Manage academic terms",
    "room.read": "View rooms",
    "room.manage": "Manage rooms",
    "timetable.manage": "Build, publish and archive timetables",
    "lesson.read": "View lesson records (held or cancelled, topic covered)",
    "lesson.manage": "Record lessons as held or cancelled",
    # Attendance (ADR-008, ADR-028). "attendance.read/create/update" date from Phase 2; "update" requests a
    # correction of a locked register, and "approve" decides it.
    "attendance.approve": "Approve or decline attendance corrections",
    # White-label (ADR-029).
    "branding.manage": "Change the school's colours, logo and favicon",
    "domain.manage": "Register, verify and remove the school's custom domains",
    # Platform services: approvals queue and documents (screen documentation).
    "approval.read": "See the central approvals queue",
    "document.read": "View documents",
    "document.download": "Download documents",
    "document.manage": "Upload and archive documents",
    # Admissions (prototype pipeline; screen documentation: Admissions, admission approvals).
    "admission.read": "View admission applications",
    "admission.manage": "Record applications, move them through the pipeline and enrol",
    "admission.approve": "Approve or decline admission offers",
    # Homework and assignments.
    "homework.read": "View homework and submissions",
    "homework.manage": "Set, edit and archive homework",
    "homework.submit": "Submit homework",
    "homework.review": "Review homework submissions",
    # Conduct: remarks and behaviour incidents.
    "remark.read": "View remarks about students",
    "remark.manage": "Write, edit and delete remarks",
    "behaviour.read": "View behaviour incidents",
    "behaviour.manage": "Report, edit and resolve behaviour incidents",
    # Examinations (exams, grade bands, approval of marks).
    "exam.read": "View exams and the grading scale",
    "exam.manage": "Create exams and mark sheets, publish results, set grade bands",
    "assessment.approve": "Approve or return mark sheets and marks corrections",
    # Fees: plans, payments, refunds.
    "fee.manage": "Manage fee plans, assign them and set scholarships",
    "fee.approve": "Approve or decline fee refunds",
    # HR: staff attendance, leave, payroll, recruitment.
    "staff_attendance.read": "View staff attendance",
    "staff_attendance.create": "Check in and out",
    "staff_attendance.manage": "Record staff attendance and set the late time",
    "leave.read": "View leave requests",
    "leave.request": "Request and cancel own leave",
    "leave.manage": "Manage leave types",
    "leave.approve": "Approve or decline leave",
    "payroll.read": "View salary structures and payslips",
    "payroll.manage": "Set salaries and run payroll",
    "recruitment.read": "View job openings and candidates",
    "recruitment.manage": "Manage job openings and candidates",
    # Transport operations.
    "transport.operate": "Run trips: start, delay, positions, arrival",
    # Hostel outpasses.
    "hostel.outpass": "Request and cancel hostel outpasses",
    "hostel.approve": "Approve or decline hostel outpasses",
    # Inventory, assets and procurement.
    "inventory.read": "View stock and assets",
    "inventory.manage": "Manage stock items, movements and assets",
    "procurement.read": "View vendors, purchase orders and invoices",
    "procurement.manage": "Manage vendors, purchase orders, receipts and invoices",
    "procurement.request": "Raise purchase requisitions",
    "procurement.approve": "Approve or decline purchase requisitions",
    # Visitor management.
    "visitor.read": "View visits",
    "visitor.register": "Register or pre-register visitors",
    "visitor.manage": "Approve visits and run the gate (scan passes)",
    # Alumni relations.
    "alumni.read": "View alumni, events and campaigns",
    "alumni.manage": "Manage alumni, events, campaigns and donations",
    # Communication: announcements, threads, complaints.
    "announcement.read": "Read announcements addressed to you",
    "announcement.manage": "Publish, edit and archive announcements",
    "message.read": "Read your parent-teacher threads",
    "message.send": "Open threads and send messages",
    "complaint.read": "View complaints and feedback",
    "complaint.create": "Raise complaints and feedback",
    "complaint.manage": "Handle complaints and view parent sentiment",
    # Learning (LMS).
    "lms.read": "View lessons, quizzes, learning paths and live classes",
    "lms.manage": "Create and publish learning content; view learning analytics",
    "lms.learn": "Track lesson progress, attempt quizzes, join live classes",
    # Monitoring Intelligence Layer.
    "monitoring.read": "See alerts, student risk, scorecards and the school pulse",
    "monitoring.manage": "Resolve alerts, run evaluations and set thresholds",
    "monitoring.ask": "Ask EduFlow questions",
}

for _codename in PERMISSIONS:
    if not CODENAME.fullmatch(_codename):  # pragma: no cover - guards future edits
        raise ValueError(f"Permission codename {_codename!r} must be '<resource>.<action>'.")

S = DataScope
_ALL_SCHOOL = dict.fromkeys(PERMISSIONS, (S.SCHOOL,))
_SCHOOL_READ = {"school.read": (S.SCHOOL,)}


# School structure that every member may see: it holds no personal data.
_STRUCTURE_READ = dict.fromkeys(
    (
        "campus.read",
        "academic_year.read",
        "term.read",
        "department.read",
        "grade.read",
        "subject.read",
        "room.read",
    ),
    (S.SCHOOL,),
)


def _school(*codenames: str) -> dict[str, tuple[DataScope, ...]]:
    return dict.fromkeys(codenames, (S.SCHOOL,))


# Role key -> (display name, {permission: scopes}). Version the matrix by editing it and running sync_rbac.
SYSTEM_ROLES: Mapping[str, tuple[str, Mapping[str, tuple[DataScope, ...]]]] = {
    "school_admin": ("School Admin", _ALL_SCHOOL),
    "principal": (
        "Principal",
        {p: (S.SCHOOL,) for p in PERMISSIONS if not p.startswith(("role.delete",))},
    ),
    "teacher": (
        "Teacher",
        {
            **_SCHOOL_READ,
            "user.read": (S.SELF,),
            "student.read": (S.SECTION, S.ASSIGNED),
            "attendance.read": (S.SECTION, S.ASSIGNED),
            "attendance.create": (S.SECTION,),
            "attendance.update": (S.SECTION,),
            "assessment.read": (S.SECTION, S.ASSIGNED),
            "assessment.create": (S.SECTION,),
            "assessment.update": (S.SECTION,),
            "timetable.read": (S.SCHOOL,),
            "report.read": (S.SECTION,),
            **_STRUCTURE_READ,
            "section.read": (S.SECTION,),
            "staff.read": (S.SELF,),
            "enrollment.read": (S.SECTION,),
            "guardian.read": (S.SECTION,),
            "teacher_assignment.read": (S.SELF, S.SECTION),
            "lesson.read": (S.SELF, S.SECTION),
            "lesson.manage": (S.SELF,),
            "document.read": (
                S.SECTION,
                S.SELF,
            ),
            "document.download": (
                S.SECTION,
                S.SELF,
            ),
            "document.manage": (S.SECTION,),
            "homework.read": (S.SECTION,),
            "homework.manage": (S.SECTION,),
            "homework.review": (S.SECTION,),
            "remark.read": (S.SECTION,),
            "remark.manage": (S.SECTION,),
            "behaviour.read": (S.SECTION,),
            "behaviour.manage": (S.SECTION,),
            "exam.read": (S.SCHOOL,),
            "staff_attendance.read": (S.SELF,),
            "staff_attendance.create": (S.SELF,),
            "leave.read": (S.SELF,),
            "leave.request": (S.SELF,),
            "payroll.read": (S.SELF,),
            "library.read": (S.SELF,),
            "procurement.request": (S.SELF,),
            "visitor.read": (S.SELF,),
            "visitor.register": (S.SELF,),
            "announcement.read": (
                S.SELF,
                S.SECTION,
            ),
            "announcement.manage": (S.SECTION,),
            "message.read": (S.SELF,),
            "message.send": (S.SECTION,),
            "lms.read": (S.SECTION,),
            "lms.manage": (S.SECTION,),
            "monitoring.read": (
                S.SELF,
                S.SECTION,
            ),
            "monitoring.ask": (S.SELF,),
        },
    ),
    "parent": (
        "Parent",
        {
            **_SCHOOL_READ,
            "user.read": (S.SELF,),
            "student.read": (S.CHILD,),
            "attendance.read": (S.CHILD,),
            "assessment.read": (S.CHILD,),
            "timetable.read": (S.CHILD,),
            "report.read": (S.CHILD,),
            "fee.read": (S.CHILD,),
            "transport.read": (S.CHILD,),
            **_STRUCTURE_READ,
            "section.read": (S.CHILD,),
            "enrollment.read": (S.CHILD,),
            "guardian.read": (S.SELF,),
            "teacher_assignment.read": (S.CHILD,),
            "lesson.read": (S.CHILD,),
            "document.read": (S.CHILD,),
            "document.download": (S.CHILD,),
            "homework.read": (S.CHILD,),
            "remark.read": (S.CHILD,),
            "behaviour.read": (S.CHILD,),
            "exam.read": (S.SCHOOL,),
            "library.read": (S.CHILD,),
            "hostel.read": (S.CHILD,),
            "hostel.outpass": (S.CHILD,),
            "visitor.read": (S.SELF,),
            "visitor.register": (S.CHILD,),
            "announcement.read": (S.SELF,),
            "message.read": (S.SELF,),
            "message.send": (S.CHILD,),
            "complaint.read": (
                S.SELF,
                S.CHILD,
            ),
            "complaint.create": (S.CHILD,),
            "lms.read": (S.CHILD,),
        },
    ),
    "student": (
        "Student",
        {
            **_SCHOOL_READ,
            "user.read": (S.SELF,),
            "student.read": (S.SELF,),
            "attendance.read": (S.SELF,),
            "assessment.read": (S.SELF,),
            "timetable.read": (S.SELF,),
            "report.read": (S.SELF,),
            **_STRUCTURE_READ,
            "section.read": (S.SELF,),
            "enrollment.read": (S.SELF,),
            "guardian.read": (S.SELF,),
            "teacher_assignment.read": (S.SELF,),
            "lesson.read": (S.SELF,),
            "document.read": (S.SELF,),
            "document.download": (S.SELF,),
            "homework.read": (S.SELF,),
            "homework.submit": (S.SELF,),
            "remark.read": (S.SELF,),
            "behaviour.read": (S.SELF,),
            "exam.read": (S.SCHOOL,),
            "fee.read": (S.SELF,),
            "library.read": (S.SELF,),
            "transport.read": (S.SELF,),
            "hostel.read": (S.SELF,),
            "hostel.outpass": (S.SELF,),
            "announcement.read": (S.SELF,),
            "message.read": (S.SELF,),
            "message.send": (S.SELF,),
            "lms.read": (S.SELF,),
            "lms.learn": (S.SELF,),
        },
    ),
    "accountant": (
        "Accountant",
        {
            **_SCHOOL_READ,
            "user.read": (S.SELF,),
            **_school("fee.read", "fee.update", "student.read", "report.read"),
            **_STRUCTURE_READ,
            **_school("section.read", "enrollment.read", "guardian.read"),
            "approval.read": (S.SCHOOL,),
            "fee.manage": (S.SCHOOL,),
            "staff_attendance.read": (S.SELF,),
            "staff_attendance.create": (S.SELF,),
            "leave.read": (S.SELF,),
            "leave.request": (S.SELF,),
            "payroll.read": (S.SCHOOL,),
            "procurement.read": (S.SCHOOL,),
            "procurement.manage": (S.SCHOOL,),
            "procurement.request": (S.SCHOOL,),
            "inventory.read": (S.SCHOOL,),
            "visitor.read": (S.SELF,),
            "visitor.register": (S.SELF,),
            "announcement.read": (S.SELF,),
            "monitoring.ask": (S.SELF,),
        },
    ),
    "hr_manager": (
        "HR Manager",
        {
            **_SCHOOL_READ,
            **_STRUCTURE_READ,
            **_school(
                "user.read", "user.update", "staff.read", "staff.update", "staff.create", "report.read"
            ),
            "approval.read": (S.SCHOOL,),
            "document.read": (S.SCHOOL,),
            "document.download": (S.SCHOOL,),
            "document.manage": (S.SCHOOL,),
            "staff_attendance.read": (S.SCHOOL,),
            "staff_attendance.create": (S.SELF,),
            "staff_attendance.manage": (S.SCHOOL,),
            "leave.read": (S.SCHOOL,),
            "leave.request": (S.SELF,),
            "leave.manage": (S.SCHOOL,),
            "leave.approve": (S.SCHOOL,),
            "payroll.read": (S.SCHOOL,),
            "payroll.manage": (S.SCHOOL,),
            "recruitment.read": (S.SCHOOL,),
            "recruitment.manage": (S.SCHOOL,),
            "procurement.request": (S.SELF,),
            "visitor.read": (S.SELF,),
            "visitor.register": (S.SELF,),
            "announcement.read": (S.SELF,),
            "monitoring.ask": (S.SELF,),
        },
    ),
    "librarian": (
        "Librarian",
        {
            **_SCHOOL_READ,
            "user.read": (S.SELF,),
            **_school("library.read", "library.manage", "student.read", "section.read", "enrollment.read"),
            **_STRUCTURE_READ,
            "staff_attendance.read": (S.SELF,),
            "staff_attendance.create": (S.SELF,),
            "leave.read": (S.SELF,),
            "leave.request": (S.SELF,),
            "payroll.read": (S.SELF,),
            "procurement.request": (S.SELF,),
            "announcement.read": (S.SELF,),
        },
    ),
    "transport_manager": (
        "Transport Manager",
        {
            **_SCHOOL_READ,
            "user.read": (S.SELF,),
            **_school(
                "transport.read", "transport.manage", "student.read", "section.read", "enrollment.read"
            ),
            **_STRUCTURE_READ,
            "staff_attendance.read": (S.SELF,),
            "staff_attendance.create": (S.SELF,),
            "leave.read": (S.SELF,),
            "leave.request": (S.SELF,),
            "payroll.read": (S.SELF,),
            "transport.operate": (S.SCHOOL,),
            "procurement.request": (S.SELF,),
            "announcement.read": (S.SELF,),
        },
    ),
    "hostel_manager": (
        "Hostel Manager",
        {
            **_SCHOOL_READ,
            "user.read": (S.SELF,),
            **_school("hostel.read", "hostel.manage", "student.read", "section.read", "enrollment.read"),
            **_STRUCTURE_READ,
            "staff_attendance.read": (S.SELF,),
            "staff_attendance.create": (S.SELF,),
            "leave.read": (S.SELF,),
            "leave.request": (S.SELF,),
            "payroll.read": (S.SELF,),
            "hostel.outpass": (S.SCHOOL,),
            "hostel.approve": (S.SCHOOL,),
            "procurement.request": (S.SELF,),
            "announcement.read": (S.SELF,),
        },
    ),
    "staff": (
        "Staff",
        {
            **_SCHOOL_READ,
            **_STRUCTURE_READ,
            "user.read": (S.SELF,),
            "timetable.read": (S.SCHOOL,),
            "staff.read": (S.SELF,),
            "document.read": (S.SELF,),
            "document.download": (S.SELF,),
            "staff_attendance.read": (S.SELF,),
            "staff_attendance.create": (S.SELF,),
            "leave.read": (S.SELF,),
            "leave.request": (S.SELF,),
            "payroll.read": (S.SELF,),
            "library.read": (S.SELF,),
            "procurement.request": (S.SELF,),
            "visitor.read": (S.SELF,),
            "visitor.register": (S.SELF,),
            "announcement.read": (S.SELF,),
        },
    ),
    "driver": (
        "Driver",
        {
            **_SCHOOL_READ,
            "user.read": (S.SELF,),
            "transport.read": (S.ASSIGNED,),
            "student.read": (S.ASSIGNED,),
            **_STRUCTURE_READ,
            "staff_attendance.read": (S.SELF,),
            "staff_attendance.create": (S.SELF,),
            "leave.read": (S.SELF,),
            "leave.request": (S.SELF,),
            "payroll.read": (S.SELF,),
            "transport.operate": (S.ASSIGNED,),
            "announcement.read": (S.SELF,),
        },
    ),
    "security": (
        "Security",
        {
            **_SCHOOL_READ,
            "user.read": (S.SELF,),
            **_STRUCTURE_READ,
            "visitor.read": (S.SCHOOL,),
            "visitor.register": (S.SCHOOL,),
            "visitor.manage": (S.SCHOOL,),
            "announcement.read": (S.SELF,),
        },
    ),
}

# Its permissions cannot be edited and it cannot be deleted, so a school can never lock itself out.
LOCKED_ROLES = frozenset({"school_admin"})
ADMIN_ROLE = "school_admin"

for _key, (_name, _grants) in SYSTEM_ROLES.items():  # pragma: no cover - guards future edits
    unknown = set(_grants) - set(PERMISSIONS)
    if unknown:
        raise ValueError(f"Role {_key!r} grants unknown permissions: {sorted(unknown)}")
