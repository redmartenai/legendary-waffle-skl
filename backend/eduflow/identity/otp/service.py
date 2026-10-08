"""Phone one-time-code sign-in (ADR-005, docs/security/otp.md).

Rules:

* 6-digit code from ``secrets``, valid for ``OTP_TTL_SECONDS`` (5 minutes), usable once.
* Only ``HMAC-SHA256(key, challenge_id:code)`` is stored. The key is derived from ``SECRET_KEY``.
* At most ``OTP_MAX_ATTEMPTS`` (5) guesses per challenge; then the challenge is dead.
* A new request for the same number invalidates earlier open challenges, and must wait
  ``OTP_RESEND_SECONDS`` after the previous one. Per-number and per-IP rate limits apply on top
  (``eduflow.identity.throttles``).
* The response is the same whether or not the number has an account. For an unknown number a challenge is
  still created, but nothing is sent and it can never be verified.
* The code never appears in logs, audit records or responses, except ``dev_code`` in local development
  (``DEBUG`` and ``OTP_ECHO_DEV_CODE`` both on; production refuses to start with the flag).
"""

from __future__ import annotations

import hashlib
import hmac
import math
import secrets
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.db import connection, transaction
from django.utils import timezone
from rest_framework.exceptions import Throttled

from eduflow.audit import services as audit
from eduflow.audit.models import Outcome
from eduflow.core.api import InvalidOtp, ServiceUnavailable
from eduflow.core.logging import get_logger
from eduflow.core.request_context import get_request_info

from ..models import OtpChallenge, OtpPurpose, User
from .providers import SmsUnavailable, get_sms_provider

log = get_logger(__name__)


@dataclass(frozen=True)
class OtpRequestResult:
    challenge_id: str
    expires_in: int
    resend_in: int
    dev_code: str | None = None


def _key(purpose: bytes) -> bytes:
    return hashlib.sha256(b"eduflow." + purpose + b"\x00" + settings.SECRET_KEY.encode()).digest()


def phone_digest(phone: str) -> str:
    return hmac.new(_key(b"otp-phone"), phone.encode(), hashlib.sha256).hexdigest()


def code_digest(challenge_id: str, code: str) -> str:
    return hmac.new(_key(b"otp-code"), f"{challenge_id}:{code}".encode(), hashlib.sha256).hexdigest()


def _generate_code() -> str:
    length = int(settings.OTP_LENGTH)
    return f"{secrets.randbelow(10**length):0{length}d}"


def request_code(phone: str) -> OtpRequestResult:
    """``phone`` must already be normalised to E.164."""
    now = timezone.now()
    digest = phone_digest(phone)
    resend_after = timedelta(seconds=int(settings.OTP_RESEND_SECONDS))

    provider = get_sms_provider()
    if not getattr(provider, "enabled", True):
        # Checked before the number is looked up, so the answer is the same for every number.
        log.error("otp_sms_provider_disabled")
        raise ServiceUnavailable()

    code = _generate_code()
    with transaction.atomic():
        # Serialise requests for one number, so parallel requests cannot all pass the cooldown check.
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", [digest])
        previous = OtpChallenge.objects.filter(phone_hash=digest).order_by("-created_at").first()
        if previous is not None and previous.created_at > now - resend_after:
            wait = (previous.created_at + resend_after - now).total_seconds()
            raise Throttled(wait=max(1, math.ceil(wait)))

        user = User.objects.filter(phone=phone, is_active=True).first()
        OtpChallenge.objects.filter(
            phone_hash=digest, consumed_at__isnull=True, invalidated_at__isnull=True
        ).update(invalidated_at=now)
        challenge = OtpChallenge.objects.create(
            phone_hash=digest,
            purpose=OtpPurpose.LOGIN,
            user=user,
            code_hash="",
            expires_at=now + timedelta(seconds=int(settings.OTP_TTL_SECONDS)),
            max_attempts=int(settings.OTP_MAX_ATTEMPTS),
            ip=get_request_info().ip,
        )
        challenge.code_hash = code_digest(str(challenge.id), code)
        challenge.save(update_fields=["code_hash"])

        delivered = None
        if user is not None:
            minutes = max(1, int(settings.OTP_TTL_SECONDS) // 60)
            message = f"{code} is your EduFlow sign-in code. It expires in {minutes} minutes."
            try:
                provider.send(phone, message)
                delivered = True
            except SmsUnavailable:
                # Answer exactly as for an unknown number: a 503 here would reveal that the account exists.
                log.error("otp_sms_delivery_failed")
                delivered = False

        audit.record(
            "auth.otp.requested",
            actor_id=user.pk if user else None,
            school_id=None,
            target_type="otp_challenge",
            target_id=challenge.id,
            metadata={"account_found": user is not None, "delivered": delivered},
        )

    echo = settings.DEBUG and settings.OTP_ECHO_DEV_CODE and user is not None
    return OtpRequestResult(
        challenge_id=str(challenge.id),
        expires_in=int(settings.OTP_TTL_SECONDS),
        resend_in=int(settings.OTP_RESEND_SECONDS),
        dev_code=code if echo else None,
    )


def verify_code(challenge_id: str, code: str) -> User:
    """Return the user for a correct, unexpired, unused code. Any failure is the same ``InvalidOtp``.

    A wrong guess is counted (and committed) before the error is raised.
    """
    now = timezone.now()
    user: User | None = None
    reason: str | None = None
    with transaction.atomic():
        challenge = (
            OtpChallenge.objects.select_for_update(of=("self",))
            .select_related("user")
            .filter(pk=challenge_id)
            .first()
        )
        if challenge is None:
            reason = "unknown"
        elif challenge.consumed_at or challenge.invalidated_at:
            reason = "used"
        elif challenge.expires_at <= now:
            reason = "expired"
        elif challenge.attempts >= challenge.max_attempts:
            reason = "locked"
        else:
            challenge.attempts += 1
            correct = hmac.compare_digest(challenge.code_hash, code_digest(str(challenge.id), code))
            if correct and challenge.user is not None and challenge.user.is_active:
                challenge.consumed_at = now
                user = challenge.user
            else:
                reason = "wrong_code"
                if challenge.attempts >= challenge.max_attempts:
                    challenge.invalidated_at = now
            challenge.save(update_fields=["attempts", "consumed_at", "invalidated_at"])

        audit.record(
            "auth.otp.verified",
            outcome=Outcome.SUCCESS if user else Outcome.FAILURE,
            actor_id=(challenge.user_id if challenge else None),
            school_id=None,
            target_type="otp_challenge",
            target_id=challenge_id if challenge else "",
            metadata={"reason": reason} if reason else None,
        )

    if user is None:
        raise InvalidOtp()
    return user
