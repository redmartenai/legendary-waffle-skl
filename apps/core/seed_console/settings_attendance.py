"""Console seed: school settings (role matrix, custom roles, role-change history, holidays) and the attendance page
(ten days of section registers shaped like the design, today's explained absences and late arrivals, correction
slips, calls already made). Idempotent under ``seed_design --only settings_attendance``."""

import random
from datetime import datetime, time, timedelta

from django.utils import timezone

from apps.academics.models import Student, StudentGuardian
from apps.accounts import permissions as perms
from apps.accounts.models import AuditLog, CustomRole, Role, RolePermission, User
from apps.approvals.models import ApprovalEvent, ApprovalRequest
from apps.approvals.services import open_request
from apps.attendance.models import AbsenceContact, AttendanceCorrection, AttendanceException, AttendanceSession, LeaveApplication

SEED = "settings_attendance"
CHRONIC = ("Harsh Vardhan", "Pooja Nambiar", "Samar Khanna", "Divya Krishnan", "Kunal Mehta", "Fatima Sayed")
LEAVE_REASONS = ("Fever and cold", "Family function out of town", "Doctor's appointment", "Stomach upset", "Grandmother unwell", "Religious ceremony at home")

CUSTOM_ROLES = (
    # key, name, description, based_on, member phones (support staff), days ago created, overrides
    ("custom-hr-manager", "HR Manager", "Staff records, leave and documents", Role.ADMIN, (205,), 106,
     {m: ("", "none") for m in perms.MODULES} | {"students": ("V", "school"), "documents": ("VCEW", "school"), "messages": ("VCW", "school"), "reports": ("VXW", "school"), "timetable": ("V", "school")}),
    ("custom-librarian", "Librarian", "Library issues and returns", Role.TEACHER, (210, 211), 150,
     {m: ("", "none") for m in perms.MODULES} | {"students": ("V", "school"), "documents": ("VCEW", "school"), "messages": ("VC", "school"), "timetable": ("V", "own")}),
    ("custom-hostel-manager", "Hostel Manager", "Boarders' attendance and messages", Role.TEACHER, (), 140,
     {m: ("", "none") for m in perms.MODULES} | {"students": ("V", "own"), "attendance": ("VCE", "own"), "messages": ("VCW", "own")}),
)

# (days ago, hour, minute, role key, module, action, to) — each becomes a "role.permission" audit entry.
ROLE_HISTORY = (
    (43, 10, 12, Role.TEACHER, "homework", "publish", True),
    (86, 16, 40, Role.ACCOUNTANT, "fees", "export", True),
    (131, 11, 30, Role.TRANSPORT_MANAGER, "reports", "export", True),
    (120, 9, 15, Role.TEACHER, "documents", "create", True),
    (121, 9, 20, Role.TEACHER, "messages", "edit", False),
)


def seed(cmd, school):
    rng = random.Random(22092026)
    _roles(cmd, school)
    _holidays(school, cmd.today)
    _registers(cmd, rng)
    _today(cmd, rng)
    _corrections(cmd, school)
    _contacts(cmd)


# ------------------------------------------------------------------ settings


def _cell(letters: str, scope: str, module: str) -> dict:
    flags = {perms.LETTERS[ch] for ch in letters}
    return {**{a: a in flags and a not in perms.NOT_APPLICABLE[module] for a in perms.ACTIONS}, "data_scope": scope}


def _roles(cmd, school):
    principal = cmd.principal
    # The seed rebuilds every role from scratch, so the history of the old matrix goes with it.
    AuditLog.objects.filter(module="roles").delete()
    RolePermission.objects.all().delete()
    CustomRole.objects.all().delete()
    for role in perms.SYSTEM_ROLES:
        for m in perms.MODULES:
            row = RolePermission(role=role, module=m, updated_by=principal)
            row.set_cell(perms.default_cell(role, m))
            row.save()

    now = timezone.now()

    def log(days_ago, h, m, action, target, summary, detail=None):
        at = datetime.combine(cmd.today - timedelta(days=days_ago), time(h, m), tzinfo=cmd.tz)
        entry = AuditLog.objects.create(
            actor=principal, action=action, module="roles", target_type="role", target_id=target, summary=summary,
            detail={"seed": SEED, **(detail or {})}, ip="10.0.4.21", device="Chrome on macOS",
        )
        AuditLog.objects.filter(pk=entry.pk).update(created_at=min(at, now))

    labels = dict(Role.choices)
    for key, name, description, based_on, phones, days_ago, overrides in CUSTOM_ROLES:
        role = CustomRole.objects.create(key=key, name=name, description=description, based_on=based_on, created_by=principal)
        members = list(User.objects.filter(phone__in=[cmd._phone(n) for n in phones]))
        role.members.set(members)
        for m in perms.MODULES:
            letters, scope = overrides[m]
            row = RolePermission(role=key, module=m, updated_by=principal)
            row.set_cell(_cell(letters, scope, m))
            row.save()
        log(days_ago, 11, 5, "role.create", key, f"{name} role created", {"based_on": based_on})
        for u in members:
            log(days_ago, 11, 9, "role.assign", key, f"{u.full_name} added to {name}")

    from apps.principal.console.settings import ACTION_LABELS, MODULE_LABELS

    for days_ago, h, m, role, module, action, to in ROLE_HISTORY:
        log(days_ago, h, m, "role.permission", role, f"{labels[role]} · {MODULE_LABELS[module]} → {ACTION_LABELS[action]} {'on' if to else 'off'}",
            {"role": role, "module": module, "action": action, "from": not to, "to": to})


def _holidays(school, today):
    year = today.year if today.month >= 4 else today.year - 1
    holidays = [
        (f"{year}-04-14", "Ambedkar Jayanti"),
        (f"{year}-05-01", "May Day"),
        (f"{year}-10-02", "Gandhi Jayanti"),
        (f"{year}-10-20", "Dussehra"),
        (f"{year}-11-09", "Diwali"),
        (f"{year}-11-24", "Guru Nanak Jayanti"),
        (f"{year}-12-25", "Christmas"),
        (f"{year + 1}-01-26", "Republic Day"),
        (f"{year + 1}-03-04", "Holi"),
    ]
    from datetime import date

    school.settings = {
        **(school.settings or {}),
        "holidays": [{"date": d, "name": n} for d, n in holidays if date.fromisoformat(d).weekday() != 6],
    }
    school.save(update_fields=["settings", "updated_at"])


# ------------------------------------------------------------------ attendance


def _protected(cmd) -> set:
    """Children whose registers other seeds shape (the demo family, the chronic absentees)."""
    ids = set(Student.objects.filter(full_name__in=CHRONIC).values_list("id", flat=True))
    for key in (("6-B", "Aarav Sharma"), ("2-A", "Diya Sharma"), ("6-B", "Kabir Khan"), ("6-B", "Zara Sheikh")):
        s = cmd.students.get(key)
        if s:
            ids.add(s.id)
    return ids


# The design's two weak sections, % present over the nine school days before today (oldest first).
SHAPES = {
    "9-A": (97, 95, 95, 89, 87, 87, 84, 84, 84),
    "7-C": (97, 94, 97, 94, 97, 94, 92, 89, 89),
}


def _registers(cmd, rng):
    """Shape the nine school days before today: most sections 91–100%, 9-A falling away, 7-C sliding."""
    days = cmd._school_days(cmd.today, 9)
    protected = _protected(cmd)
    for label, group in sorted(cmd.groups.items()):
        members = sorted(Student.objects.filter(class_group=group, is_active=True).values_list("id", flat=True))
        size = len(members)
        if not size:
            continue
        sessions = {s.date: s for s in AttendanceSession.objects.filter(class_group=group, date__in=days)}
        for i, day in enumerate(days):
            session = sessions.get(day)
            if session is None:
                continue
            local = random.Random(f"{label}:{day.isoformat()}")
            pct = SHAPES[label][i] if label in SHAPES else local.choice((91, 92, 93, 94, 94, 95, 95, 96, 96, 97, 97, 97, 98, 100))
            target = round(size * (100 - pct) / 100)
            rows = list(AttendanceException.objects.filter(session=session, status__in=["absent", "excused"]))
            fixed = [r for r in rows if r.student_id in protected]
            free = sorted((r for r in rows if r.student_id not in protected), key=lambda r: str(r.student_id))
            want = max(target - len(fixed), 0)
            for r in free[want:]:
                r.delete()
            if len(free) < want:
                taken = set(AttendanceException.objects.filter(session=session).values_list("student_id", flat=True))
                pool = [sid for sid in members if sid not in taken and sid not in protected]
                local.shuffle(pool)
                for sid in pool[: want - len(free)]:
                    AttendanceException.objects.create(session=session, student_id=sid, status="absent")


def _today(cmd, rng):
    """Today: about half the absences have a reason (approved leave), and the late arrivals cluster in Grades 7 and 9."""
    today = cmd.today
    protected = _protected(cmd)
    LeaveApplication.objects.filter(from_date=today, reason__in=LEAVE_REASONS).delete()
    absent = list(AttendanceException.objects.filter(session__date=today, status__in=["absent", "excused"]).select_related("student"))
    candidates = sorted((e for e in absent if e.student_id not in protected), key=lambda e: str(e.student_id))
    reason_count = min(31, len(candidates))
    guardians = {}
    for link in StudentGuardian.objects.filter(student_id__in=[e.student_id for e in candidates]).select_related("user").order_by("-is_primary"):
        guardians.setdefault(link.student_id, link.user)
    local = random.Random(f"leave:{today.isoformat()}")
    for i, e in enumerate(candidates):
        explained = i < reason_count
        if e.status != ("excused" if explained else "absent"):
            e.status = "excused" if explained else "absent"
            e.save(update_fields=["status", "updated_at"])
        if explained:
            LeaveApplication.objects.create(
                student_id=e.student_id, from_date=today, to_date=today, kind=local.choice(("sick", "sick", "family", "other")),
                reason=local.choice(LEAVE_REASONS), status=LeaveApplication.Status.APPROVED, applied_by=guardians.get(e.student_id),
                decided_by=e.student.class_group.class_teacher, decided_at=timezone.now(),
            )

    # Late arrivals: keep today's count, but most of them in Grades 7 and 9.
    targets = {"7-A": 2, "7-B": 2, "7-C": 2, "9-A": 1, "9-B": 2}
    sessions = {s.class_group.short_label: s for s in AttendanceSession.objects.filter(date=today).select_related("class_group")}
    late = sorted(
        AttendanceException.objects.filter(session__date=today, status="late").exclude(student_id__in=protected).select_related("session__class_group"),
        key=lambda e: str(e.student_id),
    )
    by_label = {}
    for e in late:
        by_label.setdefault(e.session.class_group.short_label, []).append(e)
    spare = [e for label, rows in sorted(by_label.items()) for e in rows[targets.get(label, 0):]]
    for label, n in targets.items():
        have = len(by_label.get(label, []))
        session = sessions.get(label)
        while have < n and spare and session:
            taken = set(AttendanceException.objects.filter(session=session).values_list("student_id", flat=True))
            pool = sorted(sid for sid in Student.objects.filter(class_group=session.class_group, is_active=True).values_list("id", flat=True) if sid not in taken and sid not in protected)
            if not pool:
                break
            spare.pop(0).delete()
            AttendanceException.objects.create(session=session, student_id=pool[len(pool) // 2], status="late", note="In at 8:15 AM")
            have += 1


SLIPS = (
    # number, class, roll, school days ago (register), from, to, teacher, asked days ago, reason, the child's name
    (419, "6-B", 22, 5, "absent", "present", "Priya Menon", 4, "Arrived 10:40 AM after a dental appointment; the parent's note is attached.", "Meera Pillai"),
    (416, "8-A", 7, 7, "absent", "excused", "Joseph Thomas", 5, "Represented the school at the inter-school science fair.", "Ritvik Sen"),
    (411, "2-A", 11, 9, "present", "absent", "Deepa Iyer", 6, "Marked present by mistake; her parent confirmed she was unwell.", "Ira Nanda"),
)


def _corrections(cmd, school):
    reasons = [s[8] for s in SLIPS]
    old = list(AttendanceCorrection.objects.filter(reason__in=reasons))
    if old:
        from django.contrib.contenttypes.models import ContentType

        ct = ContentType.objects.get_for_model(AttendanceCorrection)
        ApprovalRequest.objects.filter(target_type=ct, target_id__in=[c.id for c in old]).delete()
        for c in old:
            c.delete()
    now = timezone.now()
    for number, label, roll, ago, before, after, teacher, asked, reason, name in SLIPS:
        group = cmd.groups.get(label)
        teacher_user = cmd.staff.get(teacher)
        if group is None or teacher_user is None:
            continue
        day = cmd._school_day(cmd.today, -ago)
        session = AttendanceSession.objects.filter(class_group=group, date=day).first()
        protected = _protected(cmd)
        student = next((s for s in Student.objects.filter(class_group=group, is_active=True, roll_no__gte=roll).order_by("roll_no") if s.id not in protected), None)
        if session is None or student is None:
            continue
        if student.full_name != name:
            Student.objects.filter(pk=student.pk).update(full_name=name)
        # The register still says what the teacher wants to change.
        if before == "present":
            AttendanceException.objects.filter(session=session, student=student).delete()
        else:
            AttendanceException.objects.update_or_create(session=session, student=student, defaults={"status": before, "note": ""})
        corr = AttendanceCorrection.objects.create(
            session=session, entries=[{"student_id": str(student.id), "from": before, "to": after}], reason=reason, requested_by=teacher_user, number=number,
        )
        req = open_request(kind="attendance", target=corr, requested_by=teacher_user, summary=f"{label} register · {day:%a %d %b} · 1 change", due_on=day, notify_principal=False)
        when = datetime.combine(cmd.today - timedelta(days=asked), time(15, 20), tzinfo=cmd.tz)
        ApprovalRequest.objects.filter(pk=req.pk).update(created_at=min(when, now))
        ApprovalEvent.objects.filter(request=req).update(created_at=min(when, now))


def _contacts(cmd):
    """Two of the chronic absentees' families got an alert yesterday and haven't replied."""
    AbsenceContact.objects.all().delete()
    yesterday = cmd._school_day(cmd.today, -1)
    for label, name in (("9-A", "Pooja Nambiar"), ("4-B", "Kunal Mehta")):
        s = Student.objects.filter(full_name=name, class_group=cmd.groups.get(label)).first()
        if s:
            c = AbsenceContact.objects.create(student=s, date=yesterday, channel=AbsenceContact.Channel.ALERT, outcome=AbsenceContact.Outcome.SENT, by=cmd.principal, note="seed")
            AbsenceContact.objects.filter(pk=c.pk).update(created_at=datetime.combine(yesterday, time(9, 30), tzinfo=cmd.tz))
