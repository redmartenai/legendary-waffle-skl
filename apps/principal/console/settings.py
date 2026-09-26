"""Console: school settings (profile, calendar, roles & permissions, notifications, integrations, security, billing)."""

import re
from datetime import date, timedelta

from django.conf import settings as django_settings
from django.db import transaction
from django.db.models import Count, Q
from django.http import Http404
from django.urls import path
from django.utils import timezone
from django.utils.text import slugify
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.accounts import permissions as perms
from apps.accounts.audit import audit
from apps.accounts.models import AuditLog, CustomRole, Membership, Role, RolePermission, User
from apps.core.api import SchoolAPIView
from apps.core.utils import school_today

from .common import CONSOLE_ROLES

ROLE_LABELS = dict(Role.choices)
ACTION_LABELS = {"view": "View", "create": "Create", "edit": "Edit", "delete": "Delete / archive", "approve": "Approve", "export": "Export", "download": "Download", "publish": "Publish"}
MODULE_LABELS = {
    "students": "Students",
    "attendance": "Attendance",
    "homework": "Homework",
    "assignments": "Assignments",
    "exams": "Examinations / Marks",
    "timetable": "Timetable",
    "messages": "Messages",
    "documents": "Documents",
    "fees": "Fees",
    "transport": "Transport",
    "reports": "Reports",
}
SCOPE_LABELS = dict(RolePermission.Scope.choices)
HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
DEFAULT_ABSENCE_TEMPLATE = "{child} was marked absent today ({date}) and we haven't heard why. Please reply to the class teacher or apply for leave in the EduFlow app."


def _principal_only(request):
    # Roles, permissions and school-wide settings are the principal's call; the office can look but not change.
    if Role.PRINCIPAL not in request.roles:
        raise PermissionDenied("Only the principal can change school settings.")


# ------------------------------------------------------------------ roles


def _role_counts() -> dict[str, int]:
    rows = Membership.objects.filter(is_active=True, user__is_active=True).values("role").annotate(n=Count("user", distinct=True))
    return {r["role"]: r["n"] for r in rows}


def _last_change(key: str):
    entry = AuditLog.objects.filter(module="roles", target_type="role", target_id=key).order_by("-created_at").first()
    return entry.created_at.isoformat() if entry else None


def _roles() -> list[dict]:
    counts = _role_counts()
    out = [
        {
            "key": r.value,
            "name": ROLE_LABELS[r],
            "system": True,
            "users": counts.get(r.value, 0),
            # Fully locked roles (the principal) can't be edited at all.
            "editable": any(len(perms.locked_actions(r, m)) < len(set(perms.ACTIONS) - perms.NOT_APPLICABLE[m]) or not perms.scope_locked(r, m) for m in perms.MODULES),
        }
        for r in perms.SYSTEM_ROLES
    ]
    for cr in CustomRole.objects.annotate(n=Count("members", filter=Q(members__is_active=True))).order_by("name"):
        out.append({"key": cr.key, "name": cr.name, "system": False, "users": cr.n, "editable": True, "based_on": cr.based_on or None, "description": cr.description})
    return out


def _role_or_404(key: str) -> dict:
    role = next((r for r in _roles() if r["key"] == key), None)
    if role is None:
        raise Http404
    return role


def _matrix_payload(key: str) -> list[dict]:
    matrix = perms.role_matrix(key)
    rows = []
    for m in perms.MODULES:
        locked = perms.locked_actions(key, m)
        cell = matrix[m]
        rows.append(
            {
                "module": m,
                "cells": {a: {"allowed": cell[a], "locked": a in locked, "na": a in perms.NOT_APPLICABLE[m]} for a in perms.ACTIONS},
                "data_scope": cell["data_scope"],
                "scope_locked": perms.scope_locked(key, m),
            }
        )
    return rows


def _role_detail(key: str) -> dict:
    role = _role_or_404(key)
    members = []
    if not role["system"]:
        cr = CustomRole.objects.get(key=key)
        members = [{"id": str(u.id), "name": u.full_name, "initials": u.initials} for u in cr.members.filter(is_active=True).order_by("full_name")]
    return {**role, "changed_at": _last_change(key), "modules": _matrix_payload(key), "members": members}


def _recent_changes(limit=3) -> list[dict]:
    # Changes to what roles allow; who holds a role is in the full log.
    rows = AuditLog.objects.filter(module="roles", action__in=["role.permission", "role.create", "role.delete"]).select_related("actor")
    return [_audit_row(e) for e in rows[:limit]]


def _audit_row(e: AuditLog) -> dict:
    return {
        "id": str(e.id),
        "at": e.created_at.isoformat(),
        "actor": e.actor.full_name if e.actor else None,
        "action": e.action,
        "module": e.module,
        "summary": e.summary,
        "target_type": e.target_type,
        "target_id": e.target_id,
        "ip": e.ip,
        "device": e.device,
    }


class RolesView(SchoolAPIView):
    """GET every role with its user count and the recent role changes; POST creates a custom role."""

    allowed_roles = CONSOLE_ROLES

    def get(self, request):
        return Response({"roles": _roles(), "recent": _recent_changes(), "can_edit": Role.PRINCIPAL in request.roles})

    def post(self, request):
        _principal_only(request)
        name = str(request.data.get("name", "")).strip()
        based_on = str(request.data.get("based_on", "") or "")
        if not name:
            raise ValidationError({"name": "Name the role."})
        if len(name) > 60:
            raise ValidationError({"name": "Keep the name under 60 characters."})
        existing = {r["name"].lower() for r in _roles()}
        if name.lower() in existing:
            raise ValidationError({"name": "A role with this name already exists."})
        if based_on and based_on not in {r["key"] for r in _roles()}:
            raise ValidationError({"based_on": "Choose a role to start from."})
        base = slugify(name)[:30] or "role"
        key, n = f"custom-{base}", 2
        while CustomRole.objects.filter(key=key).exists():
            key, n = f"custom-{base}-{n}", n + 1
        with transaction.atomic():
            role = CustomRole.objects.create(key=key, name=name, description=str(request.data.get("description", ""))[:200], based_on=based_on, created_by=request.user)
            # Start from the chosen role, minus anything that role only has by platform policy.
            source = perms.role_matrix(based_on) if based_on else {m: perms.default_cell("", m) for m in perms.MODULES}
            for m, cell in source.items():
                row = RolePermission(role=key, module=m, updated_by=request.user)
                row.set_cell(cell)
                row.save()
            audit(request, "role.create", target=("role", key), module="roles", summary=f"{name} role created", detail={"based_on": based_on})
        return Response(_role_detail(key), status=201)


class RoleView(SchoolAPIView):
    """GET one role's matrix; PUT saves changed cells; DELETE removes a custom role."""

    allowed_roles = CONSOLE_ROLES

    def get(self, request, key):
        return Response(_role_detail(key))

    def put(self, request, key):
        _principal_only(request)
        role = _role_or_404(key)
        wanted = request.data.get("modules")
        if not isinstance(wanted, dict) or not wanted:
            raise ValidationError({"modules": "Nothing to save."})
        current = perms.role_matrix(key)
        changes, errors = [], {}
        for module, cell in wanted.items():
            if module not in current or not isinstance(cell, dict):
                errors[module] = "Unknown module."
                continue
            locked = perms.locked_actions(key, module)
            for action, value in cell.items():
                if action == "data_scope":
                    if value not in perms.SCOPES:
                        errors[module] = "Choose a data scope."
                    elif value != current[module]["data_scope"]:
                        if perms.scope_locked(key, module):
                            errors[module] = "This data scope is locked by platform policy."
                        else:
                            changes.append((module, "data_scope", current[module]["data_scope"], value))
                    continue
                if action not in perms.ACTIONS:
                    errors[module] = f"Unknown action {action}."
                    continue
                if bool(value) == current[module][action]:
                    continue
                if action in perms.NOT_APPLICABLE[module]:
                    errors[module] = f"{ACTION_LABELS[action]} doesn't apply to {MODULE_LABELS[module]}."
                elif action in locked:
                    errors[module] = f"{MODULE_LABELS[module]} · {ACTION_LABELS[action]} is locked by platform policy."
                else:
                    changes.append((module, action, current[module][action], bool(value)))
        if errors:
            raise ValidationError(errors)
        with transaction.atomic():
            for module in {c[0] for c in changes}:
                row = RolePermission.objects.filter(role=key, module=module).first() or RolePermission(role=key, module=module)
                cell = dict(current[module])
                for m, action, _old, new in changes:
                    if m == module:
                        cell[action] = new
                row.set_cell(cell)
                row.updated_by = request.user
                row.save()
            for module, action, old, new in changes:
                if action == "data_scope":
                    summary = f"{role['name']} · {MODULE_LABELS[module]} → {SCOPE_LABELS[new]}"
                else:
                    summary = f"{role['name']} · {MODULE_LABELS[module]} → {ACTION_LABELS[action]} {'on' if new else 'off'}"
                audit(request, "role.permission", target=("role", key), module="roles", summary=summary, detail={"role": key, "module": module, "action": action, "from": old, "to": new})
        return Response({**_role_detail(key), "saved": len(changes)})

    def delete(self, request, key):
        _principal_only(request)
        role = CustomRole.objects.filter(key=key).first()
        if role is None:
            if key in perms.SYSTEM_ROLES:
                raise ValidationError({"role": "System roles can't be deleted."})
            raise Http404
        with transaction.atomic():
            RolePermission.objects.filter(role=key).delete()
            name = role.name
            role.delete()
            audit(request, "role.delete", target=("role", key), module="roles", summary=f"{name} role deleted")
        return Response(status=204)


class RoleMembersView(SchoolAPIView):
    """Add (POST) or remove (DELETE) a staff member from a custom role."""

    allowed_roles = CONSOLE_ROLES

    def _load(self, request, key):
        _principal_only(request)
        role = CustomRole.objects.filter(key=key).first()
        if role is None:
            raise Http404
        user = User.objects.filter(
            id=request.data.get("user_id"), memberships__school=request.school, memberships__is_active=True
        ).exclude(memberships__role__in=[Role.PARENT, Role.STUDENT]).first() if _uuidish(request.data.get("user_id")) else None
        if user is None:
            raise ValidationError({"user_id": "Choose a staff member."})
        return role, user

    def post(self, request, key):
        role, user = self._load(request, key)
        role.members.add(user)
        audit(request, "role.assign", target=("role", key), module="roles", summary=f"{user.full_name} added to {role.name}")
        return Response(_role_detail(key), status=201)

    def delete(self, request, key):
        role, user = self._load(request, key)
        role.members.remove(user)
        audit(request, "role.unassign", target=("role", key), module="roles", summary=f"{user.full_name} removed from {role.name}")
        return Response(_role_detail(key))


def _uuidish(value) -> bool:
    import uuid

    try:
        uuid.UUID(str(value))
        return True
    except ValueError:
        return False


class StaffPickerView(SchoolAPIView):
    """Staff who can be given a custom role."""

    allowed_roles = CONSOLE_ROLES

    def get(self, request):
        q = (request.query_params.get("q") or "").strip()
        rows = Membership.objects.filter(is_active=True).exclude(role__in=[Role.PARENT, Role.STUDENT]).select_related("user").order_by("user__full_name")
        if q:
            rows = rows.filter(user__full_name__icontains=q)
        seen, items = set(), []
        for m in rows:
            if m.user_id in seen:
                continue
            seen.add(m.user_id)
            items.append({"id": str(m.user_id), "name": m.user.full_name, "title": m.title or ROLE_LABELS.get(m.role, m.role)})
            if len(items) >= 12:
                break
        return Response({"items": items})


# ------------------------------------------------------------------ audit log


class AuditLogView(SchoolAPIView):
    """The school's audit trail, newest first. Filters: module, action, actor, q, from, to; paged."""

    allowed_roles = CONSOLE_ROLES

    def get(self, request):
        p = request.query_params
        qs = AuditLog.objects.select_related("actor")
        if p.get("module"):
            qs = qs.filter(module=p["module"])
        if p.get("action"):
            qs = qs.filter(action=p["action"])
        if p.get("actor"):
            if not _uuidish(p["actor"]):
                raise ValidationError({"actor": "Invalid id."})
            qs = qs.filter(actor_id=p["actor"])
        if p.get("q"):
            qs = qs.filter(Q(summary__icontains=p["q"]) | Q(actor__full_name__icontains=p["q"]) | Q(action__icontains=p["q"]))
        for key, lookup in (("from", "created_at__date__gte"), ("to", "created_at__date__lte")):
            if p.get(key):
                try:
                    qs = qs.filter(**{lookup: date.fromisoformat(p[key])})
                except ValueError as exc:
                    raise ValidationError({key: "Use YYYY-MM-DD."}) from exc
        try:
            page = max(1, int(p.get("page", 1)))
            size = min(100, max(1, int(p.get("page_size", 25))))
        except ValueError as exc:
            raise ValidationError({"page": "Use a number."}) from exc
        total = qs.count()
        items = [_audit_row(e) for e in qs[(page - 1) * size : page * size]]
        modules = sorted(set(AuditLog.objects.values_list("module", flat=True)))
        actors = {}
        for aid, name in AuditLog.objects.filter(actor__isnull=False).values_list("actor_id", "actor__full_name").distinct():
            actors[str(aid)] = name
        return Response(
            {
                "items": items,
                "total": total,
                "page": page,
                "page_size": size,
                "modules": modules,
                "actors": [{"id": k, "name": v} for k, v in sorted(actors.items(), key=lambda kv: kv[1])],
            }
        )


# ------------------------------------------------------------------ the other sections


def _profile(school) -> dict:
    s = school.settings or {}
    contacts = s.get("contacts") or {}
    return {
        "name": school.name,
        "short_name": school.short_name,
        "code": school.code,
        "campus": s.get("campus") or "",
        "address": s.get("address") or "",
        "city": school.city,
        "state": school.state,
        "logo_url": school.logo_url or "",
        "primary_color": school.primary_color,
        "email": s.get("email") or "",
        "website": s.get("website") or "",
        "contacts": {"office": contacts.get("office") or "", "transport": contacts.get("transport") or ""},
        "languages": school.languages or [],
    }


def _calendar(school, today) -> dict:
    from apps.academics.models import AcademicYear

    s = school.settings or {}
    year = AcademicYear.objects.filter(is_current=True).first()
    holidays = sorted(s.get("holidays") or [], key=lambda h: h.get("date", ""))
    return {
        "academic_year": {"name": year.name, "starts_on": year.starts_on.isoformat(), "ends_on": year.ends_on.isoformat()} if year else None,
        "terms": s.get("terms") or [],
        "holidays": holidays,
        "upcoming_holidays": sum(1 for h in holidays if h.get("date", "") >= today.isoformat()),
        "week": "Monday to Saturday",
        "attendance_cutoff": school.policy("attendance", "edit_cutoff"),
    }


def _notifications(school) -> dict:
    s = (school.settings or {}).get("notifications") or {}
    return {
        "quiet_hours": school.policy("notifications", "quiet_hours"),
        "channels": _channel_status(),
        "templates": {"absence": (s.get("templates") or {}).get("absence") or DEFAULT_ABSENCE_TEMPLATE},
        "absence_alerts": bool(s.get("absence_alerts", True)),
    }


def _channel_status() -> list[dict]:
    providers = getattr(django_settings, "EDUFLOW_CHANNEL_PROVIDERS", {}) or {}
    push = bool(django_settings.EDUFLOW.get("PUSH_ENABLED"))
    out = [{"key": "in_app", "connected": True}, {"key": "push", "connected": push}]
    out += [{"key": ch, "connected": bool(providers.get(ch))} for ch in ("sms", "whatsapp", "email")]
    return out


def _integrations(school) -> list[dict]:
    from apps.staff.models import StaffAttendance
    from apps.transport.models import Vehicle

    gateway = ((school.settings or {}).get("payments") or {}).get("gateway") or ""
    providers = getattr(django_settings, "EDUFLOW_CHANNEL_PROVIDERS", {}) or {}
    trackers = Vehicle.objects.filter(has_gps_tracker=True, gps_device_id__isnull=False).count()
    vehicles = Vehicle.objects.filter(is_active=True).count()
    biometric = StaffAttendance.objects.filter(source="biometric").exists()
    return [
        {"key": "payments", "status": "live" if gateway == "razorpay" else "test" if gateway == "mock" else "off", "detail": gateway or None},
        {"key": "sms", "status": "live" if providers.get("sms") else "off", "detail": None},
        {"key": "whatsapp", "status": "live" if providers.get("whatsapp") else "off", "detail": None},
        {"key": "biometric", "status": "live" if biometric else "off", "detail": None},
        {"key": "gps", "status": "live" if trackers else "off", "detail": f"{trackers}/{vehicles}"},
    ]


def _security(school, today) -> dict:
    staff = Membership.objects.filter(is_active=True).exclude(role__in=[Role.PARENT, Role.STUDENT]).values_list("user_id", flat=True).distinct()
    with_password = User.objects.filter(id__in=staff).exclude(password__startswith="!").count()
    since = timezone.now() - timedelta(days=30)
    return {
        "sign_in": "otp",
        "two_step": False,
        "staff": len(set(staff)),
        "staff_with_password": with_password,
        "audit_entries_30d": AuditLog.objects.filter(created_at__gte=since).count(),
        "sensitive_30d": AuditLog.objects.filter(created_at__gte=since, module__in=["roles", "fees", "documents", "approvals", "reports"]).count(),
    }


def _billing(school, today) -> dict:
    from apps.academics.models import Student
    from apps.announcements.models import ChannelDelivery

    month_start = today.replace(day=1)
    sms = ChannelDelivery.objects.filter(channel="sms", created_at__date__gte=month_start)
    return {
        "plan": None,
        "students": Student.objects.filter(is_active=True).count(),
        "staff": Membership.objects.filter(is_active=True).exclude(role__in=[Role.PARENT, Role.STUDENT]).values("user").distinct().count(),
        "sms_month": sms.count(),
        "sms_delivered_month": sms.exclude(status="logged").count(),
        "sms_credits": None,
    }


class SettingsView(SchoolAPIView):
    """Everything on the settings page except the role matrix."""

    allowed_roles = CONSOLE_ROLES

    def get(self, request):
        school = request.school
        today = school_today(school)
        return Response(
            {
                "can_edit": Role.PRINCIPAL in request.roles,
                "profile": _profile(school),
                "calendar": _calendar(school, today),
                "roles": len(_roles()),
                "notifications": _notifications(school),
                "integrations": _integrations(school),
                "security": _security(school, today),
                "billing": _billing(school, today),
            }
        )


def _save_settings(school, **sections):
    s = dict(school.settings or {})
    s.update(sections)
    school.settings = s


class ProfileView(SchoolAPIView):
    allowed_roles = CONSOLE_ROLES

    def patch(self, request):
        _principal_only(request)
        school = request.school
        d = request.data
        errors = {}
        fields = {}
        for key, limit in (("name", 160), ("short_name", 40), ("city", 80), ("state", 80)):
            if key in d:
                value = str(d[key] or "").strip()
                if key == "name" and not value:
                    errors[key] = "The school needs a name."
                elif len(value) > limit:
                    errors[key] = f"Keep it under {limit} characters."
                fields[key] = value
        if "logo_url" in d:
            value = str(d["logo_url"] or "").strip()
            if value and not re.match(r"^https://\S+$", value):
                errors["logo_url"] = "Use an https:// link to the logo."
            fields["logo_url"] = value
        extra = {}
        for key in ("campus", "address", "email", "website"):
            if key in d:
                extra[key] = str(d[key] or "").strip()[:200]
        if extra.get("email") and not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", extra["email"]):
            errors["email"] = "Enter a valid email."
        contacts = dict((school.settings or {}).get("contacts") or {})
        if isinstance(d.get("contacts"), dict):
            from apps.core.utils import normalize_phone

            for key in ("office", "transport"):
                if key in d["contacts"]:
                    raw = str(d["contacts"][key] or "").strip()
                    try:
                        contacts[key] = normalize_phone(raw) if raw else ""
                    except ValueError:
                        errors[f"contacts.{key}"] = "Enter a valid phone number."
        if errors:
            raise ValidationError(errors)
        before = _profile(school)
        for k, v in fields.items():
            setattr(school, k, v)
        _save_settings(school, **extra, contacts=contacts)
        school.save()
        after = _profile(school)
        changed = [k for k in after if after[k] != before[k]]
        if changed:
            audit(request, "settings.profile", target=school, module="settings", summary=f"School profile updated: {', '.join(changed)}", detail={"fields": changed})
        return Response(after)


class CalendarView(SchoolAPIView):
    """PUT the terms and holidays."""

    allowed_roles = CONSOLE_ROLES

    def put(self, request):
        _principal_only(request)
        terms, holidays = request.data.get("terms"), request.data.get("holidays", [])
        if not isinstance(terms, list) or not terms:
            raise ValidationError({"terms": "Add at least one term."})
        if not isinstance(holidays, list):
            raise ValidationError({"holidays": "Send a list of holidays."})
        clean_terms, errors = [], {}
        for i, t in enumerate(terms):
            try:
                name = str(t.get("name", "")).strip()
                starts, ends = date.fromisoformat(t["starts_on"]), date.fromisoformat(t["ends_on"])
            except (AttributeError, KeyError, TypeError, ValueError):
                errors[f"terms.{i}"] = "Each term needs a name, a start and an end date."
                continue
            if not name:
                errors[f"terms.{i}"] = "Name the term."
            elif ends <= starts:
                errors[f"terms.{i}"] = "A term must end after it starts."
            clean_terms.append({"name": name[:40], "starts_on": starts.isoformat(), "ends_on": ends.isoformat()})
        ordered = sorted(clean_terms, key=lambda t: t["starts_on"])
        for a, b in zip(ordered, ordered[1:]):
            if b["starts_on"] <= a["ends_on"]:
                errors["terms"] = f"{a['name']} and {b['name']} overlap."
        clean_holidays = []
        for i, h in enumerate(holidays):
            try:
                day = date.fromisoformat(h["date"])
                name = str(h.get("name", "")).strip()
            except (AttributeError, KeyError, TypeError, ValueError):
                errors[f"holidays.{i}"] = "Each holiday needs a date and a name."
                continue
            if not name:
                errors[f"holidays.{i}"] = "Name the holiday."
            elif day.weekday() == 6:
                errors[f"holidays.{i}"] = "That's a Sunday; the school is closed anyway."
            clean_holidays.append({"date": day.isoformat(), "name": name[:60]})
        if len({h["date"] for h in clean_holidays}) != len(clean_holidays):
            errors["holidays"] = "Two holidays share a date."
        if errors:
            raise ValidationError(errors)
        school = request.school
        _save_settings(school, terms=ordered, holidays=sorted(clean_holidays, key=lambda h: h["date"]))
        school.save(update_fields=["settings", "updated_at"])
        audit(request, "settings.calendar", target=school, module="settings", summary=f"Academic calendar updated: {len(ordered)} terms, {len(clean_holidays)} holidays")
        return Response(_calendar(school, school_today(school)))


class NotificationSettingsView(SchoolAPIView):
    allowed_roles = CONSOLE_ROLES

    def patch(self, request):
        _principal_only(request)
        school = request.school
        current = dict((school.settings or {}).get("notifications") or {})
        d = request.data
        if "quiet_hours" in d:
            qh = d["quiet_hours"]
            if not (isinstance(qh, list) and len(qh) == 2 and all(isinstance(x, str) and HHMM.match(x) for x in qh)):
                raise ValidationError({"quiet_hours": "Use two times like 21:00 and 07:00."})
            if qh[0] == qh[1]:
                raise ValidationError({"quiet_hours": "Start and end can't be the same."})
            current["quiet_hours"] = qh
        if "absence_template" in d:
            text = str(d["absence_template"] or "").strip()
            if "{child}" not in text:
                raise ValidationError({"absence_template": "Keep {child} in the message so parents know who it's about."})
            if len(text) > 300:
                raise ValidationError({"absence_template": "Keep it under 300 characters."})
            current["templates"] = {**(current.get("templates") or {}), "absence": text}
        if "absence_alerts" in d:
            current["absence_alerts"] = bool(d["absence_alerts"])
        _save_settings(school, notifications=current)
        school.save(update_fields=["settings", "updated_at"])
        audit(request, "settings.notifications", target=school, module="settings", summary="Notification settings updated", detail={"fields": sorted(k for k in d)})
        return Response(_notifications(school))


urlpatterns = [
    path("console/settings", SettingsView.as_view()),
    path("console/settings/profile", ProfileView.as_view()),
    path("console/settings/calendar", CalendarView.as_view()),
    path("console/settings/notifications", NotificationSettingsView.as_view()),
    path("console/settings/roles", RolesView.as_view()),
    path("console/settings/roles/<slug:key>", RoleView.as_view()),
    path("console/settings/roles/<slug:key>/members", RoleMembersView.as_view()),
    path("console/settings/staff", StaffPickerView.as_view()),
    path("console/settings/audit", AuditLogView.as_view()),
]
