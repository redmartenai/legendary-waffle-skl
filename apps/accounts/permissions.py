"""Role permissions: what each role may do in each module, and on whose data.

A school's matrix lives in ``RolePermission`` rows; a role/module without a row uses ``DEFAULTS`` below, so a
school that never opened the settings page (and every test school) behaves exactly as before. Cells in
``LOCKED`` are platform policy: the settings page shows them but can't change them.

Views opt in with ``permission = ("module", "action")`` on ``apps.core.api.SchoolAPIView`` (or a dict keyed by
HTTP method); ``has_permission`` is the check.
"""

from __future__ import annotations

from .models import Role

MODULES = ("students", "attendance", "homework", "assignments", "exams", "timetable", "messages", "documents", "fees", "transport", "reports")
ACTIONS = ("view", "create", "edit", "delete", "approve", "export", "download", "publish")
SCOPES = ("school", "classes", "own", "none")

# One letter per action, so the defaults below stay readable.
LETTERS = {"V": "view", "C": "create", "E": "edit", "D": "delete", "A": "approve", "X": "export", "W": "download", "P": "publish"}

# Actions that mean nothing for a module (shown as a dash).
NOT_APPLICABLE = {
    "students": {"approve", "publish"},
    "attendance": {"publish"},
    "homework": {"approve"},
    "assignments": {"approve"},
    "exams": set(),
    "timetable": {"approve", "publish"},
    "messages": {"approve", "publish"},
    "documents": {"export"},
    "fees": set(),
    "transport": {"approve", "publish"},
    "reports": {"create", "edit", "delete", "approve", "publish"},
}

ALL = "VCEDAXWP"
FAMILY = {
    "students": ("VW", "own"),
    "attendance": ("VW", "own"),
    "homework": ("VW", "own"),
    "assignments": ("VW", "own"),
    "exams": ("VW", "own"),
    "timetable": ("VW", "own"),
    "messages": ("VCW", "own"),
    "documents": ("VW", "own"),
    "fees": ("VW", "own"),
    "transport": ("V", "own"),
    "reports": ("", "none"),
}
CREW = {m: ("", "none") for m in MODULES} | {"transport": ("V", "own"), "messages": ("VC", "own"), "timetable": ("V", "own")}

# role -> module -> (letters allowed, data scope)
DEFAULTS: dict[str, dict[str, tuple[str, str]]] = {
    Role.PRINCIPAL: {m: (ALL, "school") for m in MODULES},
    Role.ADMIN: {m: (ALL, "school") for m in MODULES},
    Role.TEACHER: {
        "students": ("VW", "classes"),
        "attendance": ("VCEXW", "classes"),
        "homework": ("VCEDWP", "classes"),
        "assignments": ("VCEDWP", "classes"),
        "exams": ("VCEXW", "classes"),
        "timetable": ("VW", "own"),
        "messages": ("VCW", "classes"),
        "documents": ("VCEW", "school"),
        "fees": ("", "none"),
        "transport": ("", "classes"),
        "reports": ("VW", "classes"),
    },
    Role.ACCOUNTANT: {
        "students": ("VXW", "school"),
        "attendance": ("V", "school"),
        "homework": ("", "none"),
        "assignments": ("", "none"),
        "exams": ("", "none"),
        "timetable": ("", "none"),
        "messages": ("VCW", "school"),
        "documents": ("VW", "school"),
        "fees": (ALL, "school"),
        "transport": ("V", "school"),
        "reports": ("VXW", "school"),
    },
    Role.TRANSPORT_MANAGER: {
        "students": ("V", "school"),
        "attendance": ("V", "school"),
        "homework": ("", "none"),
        "assignments": ("", "none"),
        "exams": ("", "none"),
        "timetable": ("V", "school"),
        "messages": ("VCW", "school"),
        "documents": ("VW", "school"),
        "fees": ("", "none"),
        "transport": ("VCEDXW", "school"),
        "reports": ("VXW", "school"),
    },
    Role.DRIVER: CREW,
    Role.ATTENDANT: CREW | {"attendance": ("V", "own")},
    Role.PARENT: FAMILY,
    Role.STUDENT: FAMILY,
}

_FAMILY_LOCKS = {m: "CEDAP" for m in MODULES} | {"messages": "EDAP"}
# role -> module -> letters locked (they keep their default value); "*" also locks the data scope.
LOCKED: dict[str, dict[str, str]] = {
    # The principal can never lock the school out of itself.
    Role.PRINCIPAL: {m: ALL + "*" for m in MODULES},
    Role.TEACHER: {
        "students": "CED",
        "attendance": "DA",
        "exams": "DAP",
        "timetable": "CED",
        "documents": "A",
        "fees": ALL + "*",
        "transport": "CED",
    },
    Role.PARENT: {m: v + "*" for m, v in _FAMILY_LOCKS.items()},
    Role.STUDENT: {m: v + "*" for m, v in _FAMILY_LOCKS.items()},
    Role.DRIVER: {m: "CEDAP" for m in MODULES},
    Role.ATTENDANT: {m: "CEDAP" for m in MODULES},
}

SYSTEM_ROLES = (
    Role.PRINCIPAL,
    Role.ADMIN,
    Role.TEACHER,
    Role.PARENT,
    Role.STUDENT,
    Role.ACCOUNTANT,
    Role.TRANSPORT_MANAGER,
    Role.DRIVER,
    Role.ATTENDANT,
)


def default_cell(role: str, module: str) -> dict:
    """The built-in permissions for a role and module; custom roles start from nothing."""
    letters, scope = DEFAULTS.get(role, {}).get(module, ("", "none"))
    flags = {LETTERS[ch]: True for ch in letters}
    return {**{a: bool(flags.get(a)) and a not in NOT_APPLICABLE[module] for a in ACTIONS}, "data_scope": scope}


def locked_actions(role: str, module: str) -> set[str]:
    letters = LOCKED.get(role, {}).get(module, "")
    return {LETTERS[ch] for ch in letters if ch in LETTERS} - NOT_APPLICABLE[module]


def scope_locked(role: str, module: str) -> bool:
    return "*" in LOCKED.get(role, {}).get(module, "")


def role_matrix(role: str) -> dict[str, dict]:
    """Effective cells for one role: stored rows over the defaults."""
    from .models import RolePermission

    base = default_cell if role in DEFAULTS else (lambda _r, m: default_cell("", m))
    matrix = {m: base(role, m) for m in MODULES}
    for row in RolePermission.objects.filter(role=role):
        if row.module in matrix:
            matrix[row.module] = row.cell()
    return matrix


def user_role_keys(request) -> set[str]:
    """System roles from the active memberships plus any custom roles the user holds."""
    keys = getattr(request, "_role_keys", None)
    if keys is None:
        from .models import CustomRole

        keys = set(request.roles) | set(CustomRole.objects.filter(members=request.user).values_list("key", flat=True))
        request._role_keys = keys
    return keys


def has_permission(request, module: str, action: str) -> bool:
    """True if any of the user's roles allows ``action`` in ``module`` with a data scope other than none."""
    from .models import RolePermission

    if module not in NOT_APPLICABLE or action not in ACTIONS or action in NOT_APPLICABLE[module]:
        return False
    keys = user_role_keys(request)
    stored = {row.role: row for row in RolePermission.objects.filter(role__in=keys, module=module)}
    for key in keys:
        cell = stored[key].cell() if key in stored else default_cell(key, module)
        if cell[action] and cell["data_scope"] != "none":
            return True
    return False


def data_scope(request, module: str) -> str:
    """The widest data scope any of the user's roles has in ``module``."""
    from .models import RolePermission

    keys = user_role_keys(request)
    stored = {row.role: row for row in RolePermission.objects.filter(role__in=keys, module=module)}
    scopes = {(stored[k].cell() if k in stored else default_cell(k, module))["data_scope"] for k in keys}
    return next((s for s in SCOPES if s in scopes), "none")
