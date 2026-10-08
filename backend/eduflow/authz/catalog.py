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
}

for _codename in PERMISSIONS:
    if not CODENAME.fullmatch(_codename):  # pragma: no cover - guards future edits
        raise ValueError(f"Permission codename {_codename!r} must be '<resource>.<action>'.")

S = DataScope
_ALL_SCHOOL = dict.fromkeys(PERMISSIONS, (S.SCHOOL,))
_SCHOOL_READ = {"school.read": (S.SCHOOL,)}


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
        },
    ),
    "accountant": (
        "Accountant",
        {
            **_SCHOOL_READ,
            "user.read": (S.SELF,),
            **_school("fee.read", "fee.update", "student.read", "report.read"),
        },
    ),
    "hr_manager": (
        "HR Manager",
        {**_SCHOOL_READ, **_school("user.read", "user.update", "staff.read", "staff.update", "report.read")},
    ),
    "librarian": (
        "Librarian",
        {**_SCHOOL_READ, "user.read": (S.SELF,), **_school("library.read", "library.manage", "student.read")},
    ),
    "transport_manager": (
        "Transport Manager",
        {
            **_SCHOOL_READ,
            "user.read": (S.SELF,),
            **_school("transport.read", "transport.manage", "student.read"),
        },
    ),
    "hostel_manager": (
        "Hostel Manager",
        {**_SCHOOL_READ, "user.read": (S.SELF,), **_school("hostel.read", "hostel.manage", "student.read")},
    ),
    "staff": ("Staff", {**_SCHOOL_READ, "user.read": (S.SELF,), "timetable.read": (S.SCHOOL,)}),
    "driver": (
        "Driver",
        {
            **_SCHOOL_READ,
            "user.read": (S.SELF,),
            "transport.read": (S.ASSIGNED,),
            "student.read": (S.ASSIGNED,),
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
