"""EduFlow platform staff: register schools, see them all, hand over and reset sign-ins. Not school-scoped."""

from collections import Counter

from django.db.models import Max
from django.http import Http404
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from apps.academics.models import AcademicYear, ClassGroup, Student
from apps.accounts.models import Membership, Role, User
from apps.accounts.views import session_payload
from apps.core.tenant import unscoped
from apps.tenancy.models import School

from . import services
from .models import CredentialIssue, SchoolInvite

STAFF_EXCLUDED = (Role.PARENT, Role.STUDENT)


class PlatformAPIView(APIView):
    """Signed-in EduFlow staff only; runs across every school."""

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        if not request.user.is_staff:
            raise PermissionDenied("This is for the EduFlow team.")

    def dispatch(self, request, *args, **kwargs):
        with unscoped():
            return super().dispatch(request, *args, **kwargs)


def _counts() -> tuple[Counter, Counter]:
    students = Counter(Student.all_objects.filter(is_active=True).values_list("school_id", flat=True))
    staff = Counter()
    for school_id, _user in Membership.all_objects.filter(is_active=True).exclude(role__in=STAFF_EXCLUDED).values_list("school_id", "user_id").distinct():
        staff[school_id] += 1
    return students, staff


def _principal(school: School) -> dict | None:
    m = Membership.all_objects.filter(school=school, role=Role.PRINCIPAL, is_active=True).select_related("user").first()
    return {"id": str(m.user_id), "name": m.user.full_name, "phone": m.user.phone} if m else None


def _row(school: School, students: Counter, staff: Counter, last: dict) -> dict:
    return {
        "id": str(school.id),
        "code": school.code,
        "name": school.name,
        "city": school.city,
        "state": school.state,
        "kind": school.kind,
        "primary_color": school.primary_color,
        "is_active": school.is_active,
        "students": students.get(school.id, 0),
        "staff": staff.get(school.id, 0),
        "principal": _principal(school),
        "created_at": school.created_at.isoformat(),
        "last_sign_in": last.get(school.id).isoformat() if last.get(school.id) else None,
    }


def _last_sign_ins() -> dict:
    rows = (
        Membership.all_objects.filter(is_active=True, user__last_login__isnull=False)
        .exclude(role__in=STAFF_EXCLUDED)
        .values("school_id")
        .annotate(last=Max("user__last_login"))
    )
    return {r["school_id"]: r["last"] for r in rows}


def _person_status(user: User) -> str:
    if user.must_change_password:
        return "temporary_password"
    if user.last_login is None:
        return "never_signed_in"
    return "active"


class OverviewView(PlatformAPIView):
    def get(self, request):
        students, staff = _counts()
        last = _last_sign_ins()
        schools = list(School.objects.select_related("organization"))
        month_start = timezone.localdate().replace(day=1)
        recent = sorted(schools, key=lambda s: s.created_at, reverse=True)[:5]
        invites = SchoolInvite.objects.filter(used_at__isnull=True, revoked_at__isnull=True, expires_at__gt=timezone.now()).select_related("school", "user")
        waiting = [
            m for m in Membership.all_objects.filter(role__in=[Role.PRINCIPAL, Role.ADMIN], is_active=True, user__must_change_password=True).select_related("school", "user")
        ]
        return Response(
            {
                "schools": len(schools),
                "active": sum(1 for s in schools if s.is_active),
                "paused": sum(1 for s in schools if not s.is_active),
                "students": sum(students.values()),
                "staff": sum(staff.values()),
                "new_this_month": sum(1 for s in schools if s.created_at.date() >= month_start),
                "recent": [_row(s, students, staff, last) for s in recent],
                "pending": [
                    {"kind": "invite", "school": i.school.name, "school_id": str(i.school_id), "name": i.user.full_name, "role": i.role, "since": i.created_at.isoformat(), "expires_at": i.expires_at.isoformat()}
                    for i in invites[:10]
                ]
                + [
                    {"kind": "temporary_password", "school": m.school.name, "school_id": str(m.school_id), "name": m.user.full_name, "role": m.role, "since": None, "expires_at": None}
                    for m in waiting[:10]
                ],
            }
        )


class SchoolsView(PlatformAPIView):
    def get(self, request):
        q = (request.query_params.get("q") or "").strip()
        state = request.query_params.get("status") or "all"
        qs = School.objects.all()
        if q:
            from django.db.models import Q

            qs = qs.filter(Q(name__icontains=q) | Q(code__icontains=q) | Q(city__icontains=q))
        if state == "active":
            qs = qs.filter(is_active=True)
        elif state == "paused":
            qs = qs.filter(is_active=False)
        students, staff = _counts()
        last = _last_sign_ins()
        return Response({"items": [_row(s, students, staff, last) for s in qs.order_by("name")]})

    def post(self, request):
        data = request.data or {}
        method = data.get("method") or "password"
        send = [c for c in (data.get("send") or []) if c in ("sms", "email")]
        school, people = services.register_school(data, request.user)
        slips = [
            services.issue_credentials(user, school, role, method, request.user, send=send) if created else services.existing_account_slip(user, school, role, request.user)
            for user, role, created in people
        ]
        return Response({"school": _detail(school), "slips": slips}, status=201)


class CheckCodeView(PlatformAPIView):
    def post(self, request):
        code = (request.data.get("code") or "").strip().upper()
        name = (request.data.get("name") or "").strip()
        valid = bool(services.CODE_RE.match(code)) if code else False
        return Response(
            {
                "code": code,
                "valid": valid,
                "available": valid and services.code_available(code),
                "suggestion": services.suggest_code(name) if name else None,
            }
        )


def _detail(school: School) -> dict:
    students, staff = _counts()
    last = _last_sign_ins()
    year = AcademicYear.all_objects.filter(school=school, is_current=True).first()
    groups = ClassGroup.all_objects.filter(school=school, academic_year=year) if year else ClassGroup.all_objects.none()
    grades = {}
    for g in groups.order_by("grade", "section"):
        grades.setdefault(g.grade, []).append(g.section)
    from apps.principal.views import _grade_key

    people = []
    for m in Membership.all_objects.filter(school=school, role__in=[Role.PRINCIPAL, Role.ADMIN]).select_related("user").order_by("role", "user__full_name"):
        people.append(
            {
                "id": str(m.user_id),
                "name": m.user.full_name,
                "phone": m.user.phone,
                "email": m.user.email or None,
                "role": m.role,
                "title": m.title,
                "is_active": m.is_active,
                "status": _person_status(m.user),
                "last_sign_in": m.user.last_login.isoformat() if m.user.last_login else None,
                "invite_pending": SchoolInvite.objects.filter(user=m.user, school=school, used_at__isnull=True, revoked_at__isnull=True, expires_at__gt=timezone.now()).exists(),
            }
        )
    history = [
        {
            "id": str(i.id),
            "name": i.user.full_name,
            "role": i.role,
            "method": i.method,
            "reason": i.reason,
            "delivered": i.delivered,
            "by": i.issued_by.full_name if i.issued_by else None,
            "at": i.created_at.isoformat(),
        }
        for i in CredentialIssue.objects.filter(school=school).select_related("user", "issued_by")[:20]
    ]
    return {
        **_row(school, students, staff, last),
        "short_name": school.short_name,
        "accent_color": school.accent_color,
        "languages": school.languages,
        "organization": school.organization.name,
        "campus": (school.settings or {}).get("campus"),
        "address": (school.settings or {}).get("address"),
        "office_phone": ((school.settings or {}).get("contacts") or {}).get("office"),
        "year": {"name": year.name, "starts_on": year.starts_on.isoformat(), "ends_on": year.ends_on.isoformat()} if year else None,
        "terms": (school.settings or {}).get("terms") or [],
        "grades": [{"grade": g, "sections": grades[g]} for g in sorted(grades, key=_grade_key)],
        "sections": groups.count(),
        "people": people,
        "history": history,
    }


def _school(pk) -> School:
    school = School.objects.filter(pk=pk).first()
    if not school:
        raise Http404
    return school


class SchoolDetailView(PlatformAPIView):
    def get(self, request, pk):
        return Response(_detail(_school(pk)))

    def patch(self, request, pk):
        school = _school(pk)
        data = request.data or {}
        fields = []
        for key in ("name", "short_name", "city", "state"):
            if key in data:
                value = (data.get(key) or "").strip()
                if key == "name" and not value:
                    raise ValidationError({"name": "Enter the school's name."})
                setattr(school, key, value)
                fields.append(key)
        for key in ("primary_color", "accent_color"):
            if key in data:
                value = data.get(key) or ""
                if not (len(value) == 7 and value.startswith("#")):
                    raise ValidationError({key: "Use a hex colour like #3446C8."})
                setattr(school, key, value)
                fields.append(key)
        settings_changed = False
        new_settings = dict(school.settings or {})
        for key in ("campus", "address"):
            if key in data:
                new_settings[key] = (data.get(key) or "").strip()
                settings_changed = True
        if "office_phone" in data:
            new_settings["contacts"] = {**(new_settings.get("contacts") or {}), "office": (data.get("office_phone") or "").strip()}
            settings_changed = True
        if settings_changed:
            school.settings = new_settings
            fields.append("settings")
        if "is_active" in data:
            school.is_active = bool(data["is_active"])
            fields.append("is_active")
        if fields:
            school.save(update_fields=[*fields, "updated_at"])
        return Response(_detail(school))


class SchoolPeopleView(PlatformAPIView):
    """Add another principal or school admin to a school."""

    def post(self, request, pk):
        school = _school(pk)
        data = request.data or {}
        role = data.get("role") or Role.ADMIN
        if role not in (Role.PRINCIPAL, Role.ADMIN):
            raise ValidationError({"role": "Choose principal or school admin."})
        from django.db import transaction

        from apps.core.tenant import use_school

        with transaction.atomic(), use_school(school):
            user, created = services._person(data, "person")
            Membership.objects.get_or_create(school=school, user=user, role=role, defaults={"title": data.get("title") or ("Principal" if role == Role.PRINCIPAL else "School admin")})
            send = [c for c in (data.get("send") or []) if c in ("sms", "email")]
            slip = (
                services.issue_credentials(user, school, role, data.get("method") or "password", request.user, reason="added", send=send)
                if created
                else services.existing_account_slip(user, school, role, request.user, reason="added")
            )
        return Response({"school": _detail(school), "slip": slip}, status=201)


class CredentialsView(PlatformAPIView):
    """Reset someone's sign-in for a school: a new temporary password or a fresh invite link."""

    def post(self, request, pk, user_id):
        school = _school(pk)
        membership = Membership.all_objects.filter(school=school, user_id=user_id, role__in=[Role.PRINCIPAL, Role.ADMIN]).select_related("user").first()
        if not membership:
            raise Http404
        send = [c for c in (request.data.get("send") or []) if c in ("sms", "email")]
        slip = services.issue_credentials(membership.user, school, membership.role, request.data.get("method") or "password", request.user, reason="reset", send=send)
        return Response({"school": _detail(school), "slip": slip})


class InviteView(APIView):
    """The public page behind an invite link: who it's for, then set a password and sign in."""

    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "invite"

    def get(self, request, token):
        with unscoped():
            invite = services.invite_for(token)
            return Response(
                {
                    "school": {"name": invite.school.name, "code": invite.school.code, "primary_color": invite.school.primary_color},
                    "person": {"name": invite.user.full_name, "phone": invite.user.phone, "role": invite.role},
                    "expires_at": invite.expires_at.isoformat(),
                }
            )

    def post(self, request, token):
        with unscoped():
            user = services.accept_invite(token, request.data.get("password") or "")
            return Response(session_payload(user, remember=True))
