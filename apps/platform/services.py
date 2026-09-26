"""Register schools and hand their principals/admins a way to sign in."""

import hashlib
import re
import secrets
from datetime import date, timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.text import slugify
from rest_framework.exceptions import ValidationError

from apps.academics.models import AcademicYear, ClassGroup
from apps.accounts.models import Membership, Role, User
from apps.core.tenant import unscoped, use_school
from apps.core.utils import normalize_phone
from apps.tenancy.models import Organization, School

from .models import CredentialIssue, SchoolInvite

INVITE_DAYS = 3
# No 0/O, 1/l/I: the slip is read aloud and typed on phones.
PASSWORD_ALPHABET = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_RE = re.compile(r"^[A-Z0-9]{3,12}$")
STAFF_ROLES = (Role.PRINCIPAL, Role.ADMIN)


def temporary_password(length: int = 12) -> str:
    return "".join(secrets.choice(PASSWORD_ALPHABET) for _ in range(length))


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def suggest_code(name: str) -> str:
    """"Sunrise Public School, Pune" → "SPSPUNE"-style: initials, then padded from the first word."""
    words = [w for w in re.split(r"[^A-Za-z0-9]+", name.upper()) if w]
    base = "".join(w[0] for w in words)[:6] or "SCHOOL"
    if len(base) < 3 and words:
        base = (words[0][:6]).ljust(3, "X")
    with unscoped():
        taken = set(School.objects.filter(code__startswith=base).values_list("code", flat=True))
    if base not in taken:
        return base
    n = 2
    while f"{base}{n}" in taken:
        n += 1
    return f"{base}{n}"


def code_available(code: str) -> bool:
    with unscoped():
        return not School.objects.filter(code=code).exists()


def _person(data: dict, field: str) -> tuple[User, bool]:
    """Find the user by phone or create them. Returns (user, created)."""
    try:
        phone = normalize_phone(data.get("phone"))
    except ValueError as exc:
        raise ValidationError({f"{field}.phone": str(exc)}) from None
    name = (data.get("name") or "").strip()
    if not name:
        raise ValidationError({f"{field}.name": "Enter their full name."})
    email = (data.get("email") or "").strip()
    user = User.objects.filter(phone=phone).first()
    if user:
        return user, False
    return User.objects.create_user(phone, name, email=email), True


def _years(data: dict) -> tuple[date, date, list[dict]]:
    try:
        starts, ends = date.fromisoformat(data["starts_on"]), date.fromisoformat(data["ends_on"])
    except (KeyError, TypeError, ValueError):
        raise ValidationError({"year": "Enter the academic year's start and end dates."}) from None
    if ends <= starts:
        raise ValidationError({"year": "The year must end after it starts."})
    terms = []
    for i, term in enumerate(data.get("terms") or []):
        try:
            t_start, t_end = date.fromisoformat(term["starts_on"]), date.fromisoformat(term["ends_on"])
        except (KeyError, TypeError, ValueError):
            raise ValidationError({f"terms.{i}": "Each term needs start and end dates."}) from None
        if not (starts <= t_start < t_end <= ends):
            raise ValidationError({f"terms.{i}": "Terms must sit inside the academic year."})
        terms.append({"name": (term.get("name") or f"Term {i + 1}").strip(), "starts_on": t_start.isoformat(), "ends_on": t_end.isoformat()})
    return starts, ends, terms


@transaction.atomic
def register_school(data: dict, by: User) -> tuple[School, list[tuple[User, str, bool]]]:
    """Create the school, its year and sections, and its principal (+ optional admin), all or nothing.

    Returns the school and [(user, role, created)] so the caller can issue credentials to new accounts.
    """
    school_data = data.get("school") or {}
    name = (school_data.get("name") or "").strip()
    if not name:
        raise ValidationError({"school.name": "Enter the school's name."})
    code = (school_data.get("code") or "").strip().upper()
    if not CODE_RE.match(code):
        raise ValidationError({"school.code": "Use 3–12 letters or digits, e.g. SUNRISE."})
    if not code_available(code):
        raise ValidationError({"school.code": "That code is already taken."})
    starts, ends, terms = _years(data.get("year") or {})
    grades = data.get("grades") or []
    if not grades:
        raise ValidationError({"grades": "Add at least one grade with a section."})

    with unscoped():
        org_name = (school_data.get("organization") or name).strip()
        slug = slugify(org_name)[:70] or code.lower()
        org = Organization.objects.filter(slug=slug).first() or Organization.objects.create(name=org_name, slug=slug)
        try:
            school = School.objects.create(
                organization=org,
                code=code,
                name=name,
                short_name=(school_data.get("short_name") or "").strip()[:40],
                kind=school_data.get("kind") or School.Kind.SCHOOL,
                city=(school_data.get("city") or "").strip(),
                state=(school_data.get("state") or "").strip(),
                primary_color=school_data.get("primary_color") or "#3446C8",
                accent_color=school_data.get("accent_color") or "#C9571F",
                languages=school_data.get("languages") or ["en"],
                settings={
                    "campus": (school_data.get("campus") or "").strip() or "Main Campus",
                    "address": (school_data.get("address") or "").strip(),
                    "contacts": {"office": (school_data.get("office_phone") or "").strip()},
                    "terms": terms,
                },
            )
        except IntegrityError:
            raise ValidationError({"school.code": "That code is already taken."}) from None

    people = []
    with use_school(school):
        year_name = (data.get("year") or {}).get("name") or f"{starts.year}–{str(ends.year)[-2:]}"
        year = AcademicYear.objects.create(name=year_name, starts_on=starts, ends_on=ends, is_current=True)
        seen = set()
        for row in grades:
            grade = str(row.get("grade") or "").strip()
            sections = [str(s).strip().upper() for s in row.get("sections") or [] if str(s).strip()]
            if not grade or not sections:
                continue
            for section in sections:
                if (grade, section) in seen:
                    continue
                seen.add((grade, section))
                ClassGroup.objects.create(academic_year=year, grade=grade, section=section)
        if not seen:
            raise ValidationError({"grades": "Add at least one grade with a section."})

        for field, role, title in (("principal", Role.PRINCIPAL, "Principal"), ("admin", Role.ADMIN, "School admin")):
            person = data.get(field)
            if not person or (field == "admin" and not (person.get("name") or person.get("phone"))):
                if field == "principal":
                    raise ValidationError({"principal.name": "Every school needs a principal."})
                continue
            user, created = _person(person, field)
            Membership.objects.get_or_create(school=school, user=user, role=role, defaults={"title": person.get("title") or title})
            people.append((user, role, created))
    return school, people


def _deliver(user: User, school: School, text: str, channels: list[str]) -> dict:
    from apps.notifications.channels import address_for, provider_for

    delivered = {}
    for channel in channels:
        address = address_for(channel, user)
        if not address:
            delivered[channel] = "no_address"
            continue
        provider = provider_for(channel)
        provider.send(channel, address, text)
        delivered[channel] = getattr(provider, "delivered_status", "sent")
    return delivered


def issue_credentials(user: User, school: School, role: str, method: str, by: User, *, reason: str = "created", send: list[str] | None = None) -> dict:
    """Give ``user`` a way into ``school``: a temporary password (they must change it) or an invite link.

    The secret is returned once in the result and never stored in plain text.
    """
    if method not in ("password", "invite"):
        raise ValidationError({"method": "Choose a temporary password or an invite link."})
    now = timezone.now()
    SchoolInvite.objects.filter(user=user, used_at__isnull=True, revoked_at__isnull=True).update(revoked_at=now)
    sign_in = f"{settings.WEB_URL}/sign-in?school={school.code}"
    slip = {
        "school": {"id": str(school.id), "name": school.name, "code": school.code},
        "person": {"id": str(user.id), "name": user.full_name, "phone": user.phone, "email": user.email or None, "role": role},
        "method": method,
        "sign_in_url": sign_in,
        "password": None,
        "invite_url": None,
        "expires_at": None,
    }
    if method == "password":
        password = temporary_password()
        user.set_password(password)
        user.must_change_password = True
        user.save(update_fields=["password", "must_change_password"])
        slip["password"] = password
        text = f"EduFlow: you're set up at {school.name}. Sign in at {sign_in} with {user.phone} and temporary password {password}. You'll choose your own password next."
    else:
        token = secrets.token_urlsafe(32)
        expires = now + timedelta(days=INVITE_DAYS)
        SchoolInvite.objects.create(school=school, user=user, role=role, token_hash=token_hash(token), expires_at=expires, created_by=by)
        slip["invite_url"] = f"{settings.WEB_URL}/invite/{token}"
        slip["expires_at"] = expires.isoformat()
        text = f"EduFlow: you're invited to {school.name}. Set your password at {slip['invite_url']} (valid {INVITE_DAYS} days)."
    delivered = _deliver(user, school, text, send or [])
    issue = CredentialIssue.objects.create(school=school, user=user, role=role, method=method, reason=reason, delivered=delivered, issued_by=by)
    slip["issue_id"] = str(issue.id)
    slip["delivered"] = delivered
    return slip


def existing_account_slip(user: User, school: School, role: str, by: User, reason: str = "created") -> dict:
    """Someone who already has an EduFlow account keeps their password; record that they were added."""
    issue = CredentialIssue.objects.create(school=school, user=user, role=role, method=CredentialIssue.Method.EXISTING, reason=reason, issued_by=by)
    return {
        "school": {"id": str(school.id), "name": school.name, "code": school.code},
        "person": {"id": str(user.id), "name": user.full_name, "phone": user.phone, "email": user.email or None, "role": role},
        "method": "existing",
        "sign_in_url": f"{settings.WEB_URL}/sign-in?school={school.code}",
        "password": None,
        "invite_url": None,
        "expires_at": None,
        "issue_id": str(issue.id),
        "delivered": {},
    }


def invite_for(token: str) -> SchoolInvite:
    invite = SchoolInvite.objects.select_related("school", "user").filter(token_hash=token_hash(token or "")).first()
    if not invite or invite.used_at or invite.revoked_at or invite.expires_at <= timezone.now() or not invite.school.is_active:
        raise ValidationError({"token": "This invite link has expired or was already used. Ask EduFlow for a new one."})
    return invite


@transaction.atomic
def accept_invite(token: str, password: str) -> User:
    from django.contrib.auth.password_validation import validate_password

    invite = invite_for(token)
    user = invite.user
    if len(password or "") < 10:
        raise ValidationError({"password": "Use at least 10 characters."})
    try:
        validate_password(password, user)
    except Exception as exc:
        raise ValidationError({"password": list(getattr(exc, "messages", [str(exc)]))}) from None
    user.set_password(password)
    user.must_change_password = False
    user.save(update_fields=["password", "must_change_password"])
    invite.used_at = timezone.now()
    invite.save(update_fields=["used_at"])
    return user
