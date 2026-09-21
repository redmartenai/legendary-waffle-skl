import hashlib
import hmac
import logging
import secrets
from datetime import timedelta

from django.conf import settings
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
    message = f"{school_id}:{challenge_phone}:{code}".encode()
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def send_sms_otp(school, phone: str, code: str) -> None:
    """Send the sign-in code through the school's DLT-registered SMS sender.

    Each school registers its own principal entity and sender ID (TRAI DLT);
    EduFlow's own sender is used only for platform OTPs. The provider adapter
    plugs in here. In development the code is only logged.
    """
    masked = phone[-4:].rjust(len(phone), "•")
    if _config()["OTP_DEV_ECHO"]:
        logger.info("OTP for %s at %s: %s", masked, school.code, code)
        return
    # Never write a live code to the logs: anyone who can read them could sign in as that user.
    logger.error("No SMS provider configured: sign-in code for %s at %s was not sent", masked, school.code)


def request_otp(school, raw_phone: str, ip: str | None = None) -> tuple[OtpChallenge, str | None]:
    try:
        phone = normalize_phone(raw_phone)
    except ValueError as exc:
        raise ValidationError({"phone": str(exc)}) from exc

    since = timezone.now() - timedelta(hours=1)
    recent = OtpChallenge.objects.filter(phone=phone, created_at__gte=since).count()
    if recent >= _config()["OTP_MAX_PER_HOUR"]:
        raise Throttled(wait=3600, detail="Too many codes requested. Try again later.")

    with unscoped():
        is_member = Membership.all_objects.filter(
            school=school, user__phone=phone, user__is_active=True, is_active=True
        ).exists()

    code = f"{secrets.randbelow(10**6):06d}"
    challenge = OtpChallenge.objects.create(
        school=school,
        phone=phone,
        code_hash=_hash_code(phone, school.id, code),
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
        user = (
            User.objects.filter(
                phone=challenge.phone,
                is_active=True,
                memberships__school=challenge.school,
                memberships__is_active=True,
            )
            .distinct()
            .first()
        )
    if user is None:
        challenge.save(update_fields=["attempts", "updated_at"])
        raise invalid

    challenge.consumed_at = timezone.now()
    challenge.save(update_fields=["attempts", "consumed_at", "updated_at"])
    return user
