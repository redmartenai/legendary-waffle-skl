import hashlib
import hmac
import logging
import secrets
from datetime import timedelta

from django.conf import settings
from django.db.models import Q
from django.utils import timezone
from rest_framework.exceptions import Throttled, ValidationError

from apps.core.tenant import unscoped
from apps.core.utils import normalize_phone

from .models import Membership, OtpChallenge, User

logger = logging.getLogger("apps.accounts")


def _config():
    return settings.EDUFLOW


def _hash_code(challenge_phone: str, school_id, code: str) -> str:
    key = settings.SECRET_KEY.encode()
    message = f"{school_id or ''}:{challenge_phone}:{code}".encode()
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def send_sms_otp(school, phone: str, code: str) -> None:  # school may be None (phone-first sign-in)
    """Send the sign-in code through the school's DLT-registered SMS sender.

    Each school registers its own principal entity and sender ID (TRAI DLT);
    EduFlow's own sender is used only for platform OTPs. The provider adapter
    plugs in here. In development the code is only logged.
    """
    masked = phone[-4:].rjust(len(phone), "•")
    where = school.code if school else "EduFlow"
    if _config()["OTP_DEV_ECHO"]:
        logger.info("OTP for %s at %s: %s", masked, where, code)
        return
    # Never write a live code to the logs: anyone who can read them could sign in as that user.
    logger.error("No SMS provider configured: sign-in code for %s at %s was not sent", masked, where)


def request_otp(school, raw_phone: str, ip: str | None = None) -> tuple[OtpChallenge, str | None]:
    """Create a sign-in code. `school=None` is phone-first: any school the number belongs to."""
    try:
        phone = normalize_phone(raw_phone)
    except ValueError as exc:
        raise ValidationError({"phone": str(exc)}) from exc

    since = timezone.now() - timedelta(hours=1)
    recent = OtpChallenge.objects.filter(phone=phone, created_at__gte=since).count()
    if recent >= _config()["OTP_MAX_PER_HOUR"]:
        raise Throttled(wait=3600, detail="Too many codes requested. Try again later.")

    with unscoped():
        members = Membership.all_objects.filter(
            user__phone=phone, user__is_active=True, is_active=True, school__is_active=True
        )
        if school is not None:
            members = members.filter(school=school)
        is_member = members.exists()

    code = f"{secrets.randbelow(10**6):06d}"
    challenge = OtpChallenge.objects.create(
        school=school,
        phone=phone,
        code_hash=_hash_code(phone, school.id if school else None, code),
        expires_at=timezone.now() + timedelta(seconds=_config()["OTP_TTL_SECONDS"]),
        request_ip=ip,
    )
    # The response is identical whether or not the number is registered, so the
    # endpoint can't be used to discover who studies or works at a school.
    if is_member:
        send_sms_otp(school, phone, code)
        return challenge, code
    return challenge, None


def verify_otp(challenge_id, code: str) -> User:
    challenge = OtpChallenge.objects.select_related("school").filter(id=challenge_id).first()
    invalid = ValidationError({"code": "That code isn't right. Check the SMS and try again."})
    if challenge is None or challenge.consumed_at is not None:
        raise invalid
    if challenge.expires_at < timezone.now():
        raise ValidationError({"code": "This code has expired. Request a new one."})
    if challenge.attempts >= _config()["OTP_MAX_ATTEMPTS"]:
        raise ValidationError({"code": "Too many attempts. Request a new code."})

    challenge.attempts += 1
    expected = _hash_code(challenge.phone, challenge.school_id, str(code).strip())
    if not hmac.compare_digest(expected, challenge.code_hash):
        challenge.save(update_fields=["attempts", "updated_at"])
        raise invalid

    with unscoped():
        users = User.objects.filter(phone=challenge.phone, is_active=True, memberships__is_active=True)
        if challenge.school_id:
            users = users.filter(memberships__school=challenge.school)
        user = users.distinct().first()
    if user is None:
        challenge.save(update_fields=["attempts", "updated_at"])
        raise invalid

    challenge.consumed_at = timezone.now()
    challenge.save(update_fields=["attempts", "consumed_at", "updated_at"])
    return user


def authenticate_password(identifier: str, password: str, school=None) -> User:
    """Email-or-phone + password sign-in (the Principal web app). One generic error for every failure."""
    invalid = ValidationError({"password": "That email, number or password isn't right."})
    identifier = (identifier or "").strip()
    if not identifier or not password:
        raise invalid
    with unscoped():
        if school is not None:
            users = User.objects.filter(is_active=True, memberships__is_active=True, memberships__school__is_active=True, memberships__school=school)
        else:
            # Anyone with an active school, or EduFlow platform staff (who belong to no school).
            users = User.objects.filter(is_active=True).filter(
                Q(memberships__is_active=True, memberships__school__is_active=True) | Q(is_staff=True)
            )
        if "@" in identifier:
            users = users.filter(email__iexact=identifier)
        else:
            try:
                users = users.filter(phone=normalize_phone(identifier))
            except ValueError:
                raise invalid from None
        candidates = list(users.distinct()[:2])
    # An email shared by two accounts is ambiguous: refuse rather than guess.
    if len(candidates) != 1 or not candidates[0].has_usable_password() or not candidates[0].check_password(password):
        raise invalid
    return candidates[0]
