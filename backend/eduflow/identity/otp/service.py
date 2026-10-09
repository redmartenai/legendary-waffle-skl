"""One-time codes: phone sign-in (ADR-005) and invitation verification (ADR-025). docs/security/otp.md.

Rules, shared by every purpose:

* 6-digit code from ``secrets``, valid for ``OTP_TTL_SECONDS`` (5 minutes), usable once.
* Only ``HMAC-SHA256(key, challenge_id:code)`` is stored. The key is derived from ``SECRET_KEY``.
* At most ``OTP_MAX_ATTEMPTS`` (5) guesses per challenge; then the challenge is dead.
* A new code for the same address and purpose invalidates earlier open ones, and must wait
  ``OTP_RESEND_SECONDS`` after the previous one (serialised by a per-address advisory lock). Rate limits per
  address and per IP apply on top (``eduflow.identity.throttles``).
* A challenge verifies only its own purpose and subject: a sign-in code never accepts an invitation, and an
  invitation code never accepts a different invitation.
* The code never appears in logs, audit records or responses, except ``dev_code`` in local development
  (``DEBUG`` and ``OTP_ECHO_DEV_CODE`` both on; production refuses to start with the flag).

Sign-in specifics: the response is the same whether or not the number has an account. For an unknown number
a challenge is still created, but nothing is sent and it can never be verified.
"""

from __future__ import annotations

import hashlib
import hmac
import math
import secrets
import uuid
from collections.abc import Callable
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

from ..delivery import Channel, DeliveryUnavailable, channel_enabled, deliver
from ..models import OtpChallenge, OtpPurpose, User

log = get_logger(__name__)


@dataclass(frozen=True)
class OtpRequestResult:
    challenge_id: str
    expires_in: int
    resend_in: int
    dev_code: str | None = None


def _key(purpose: bytes) -> bytes:
    return hashlib.sha256(b"eduflow." + purpose + b"\x00" + settings.SECRET_KEY.encode()).digest()


def address_digest(channel: str, address: str) -> str:
    return hmac.new(_key(b"otp-address"), f"{channel}:{address}".encode(), hashlib.sha256).hexdigest()


def phone_digest(phone: str) -> str:
    return address_digest(Channel.PHONE, phone)


def code_digest(challenge_id: str, code: str) -> str:
    return hmac.new(_key(b"otp-code"), f"{challenge_id}:{code}".encode(), hashlib.sha256).hexdigest()


def _generate_code() -> str:
    length = int(settings.OTP_LENGTH)
    return f"{secrets.randbelow(10**length):0{length}d}"


def _ttl_minutes() -> int:
    return max(1, int(settings.OTP_TTL_SECONDS) // 60)


def _issue(
    *,
    purpose: OtpPurpose,
    channel: str,
    address: str,
    user: User | None,
    subject_id: uuid.UUID | None,
    send: Callable[[str], bool],
) -> tuple[OtpChallenge, str, bool]:
    """Create a challenge for ``address`` and hand its code to ``send``. Returns (challenge, code, delivered).

    Runs in the caller's transaction. ``send`` returns whether the message was handed to a provider.
    """
    now = timezone.now()
    digest = address_digest(channel, address)
    resend_after = timedelta(seconds=int(settings.OTP_RESEND_SECONDS))
    # Serialise requests for one address, so parallel requests cannot all pass the cooldown check.
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", [digest])
    same_address = OtpChallenge.objects.filter(address_hash=digest, purpose=purpose)
    if purpose == OtpPurpose.INVITATION:
        # One address may hold invitations from several schools: codes and cooldowns are per invitation.
        same_address = same_address.filter(subject_id=subject_id)
    previous = same_address.order_by("-created_at").first()
    if previous is not None and previous.created_at > now - resend_after:
        wait = (previous.created_at + resend_after - now).total_seconds()
        raise Throttled(wait=max(1, math.ceil(wait)))

    same_address.filter(consumed_at__isnull=True, invalidated_at__isnull=True).update(invalidated_at=now)
    challenge = OtpChallenge.objects.create(
        address_hash=digest,
        purpose=purpose,
        subject_id=subject_id,
        user=user,
        code_hash="",
        expires_at=now + timedelta(seconds=int(settings.OTP_TTL_SECONDS)),
        max_attempts=int(settings.OTP_MAX_ATTEMPTS),
        ip=get_request_info().ip,
    )
    code = _generate_code()
    challenge.code_hash = code_digest(str(challenge.id), code)
    challenge.save(update_fields=["code_hash"])
    return challenge, code, send(code)


def _consume(
    challenge_id: str, code: str, *, purpose: OtpPurpose, subject_id: uuid.UUID | None
) -> tuple[OtpChallenge | None, str | None]:
    """Check ``code`` against the challenge (locked). Returns (challenge, failure reason or None).

    A wrong guess is counted, and the challenge dies at the attempt limit. A challenge of another purpose or
    subject is treated exactly like an unknown one.
    """
    now = timezone.now()
    challenge = (
        OtpChallenge.objects.select_for_update(of=("self",))
        .select_related("user")
        .filter(pk=challenge_id, purpose=purpose, subject_id=subject_id)
        .first()
    )
    if challenge is None:
        return None, "unknown"
    if challenge.consumed_at or challenge.invalidated_at:
        return challenge, "used"
    if challenge.expires_at <= now:
        return challenge, "expired"
    if challenge.attempts >= challenge.max_attempts:
        return challenge, "locked"
    challenge.attempts += 1
    reason = None
    if hmac.compare_digest(challenge.code_hash, code_digest(str(challenge.id), code)):
        challenge.consumed_at = now
    else:
        reason = "wrong_code"
        if challenge.attempts >= challenge.max_attempts:
            challenge.invalidated_at = now
    challenge.save(update_fields=["attempts", "consumed_at", "invalidated_at"])
    return challenge, reason


def _result(challenge: OtpChallenge, code: str, *, echo: bool) -> OtpRequestResult:
    return OtpRequestResult(
        challenge_id=str(challenge.id),
        expires_in=int(settings.OTP_TTL_SECONDS),
        resend_in=int(settings.OTP_RESEND_SECONDS),
        dev_code=code if echo and settings.DEBUG and settings.OTP_ECHO_DEV_CODE else None,
    )


# ------------------------------------------------------------------------------------------------ sign-in
def request_code(phone: str) -> OtpRequestResult:
    """``phone`` must already be normalised to E.164."""
    if not channel_enabled(Channel.PHONE):
        # Checked before the number is looked up, so the answer is the same for every number.
        log.error("otp_sms_provider_disabled")
        raise ServiceUnavailable()

    with transaction.atomic():
        user = User.objects.filter(phone=phone, is_active=True).first()

        def send(code: str) -> bool:
            if user is None:
                return False
            message = f"{code} is your EduFlow sign-in code. It expires in {_ttl_minutes()} minutes."
            try:
                deliver(Channel.PHONE, phone, subject="", body=message)
            except DeliveryUnavailable:
                # Answer exactly as for an unknown number: a 503 here would reveal that the account exists.
                return False
            return True

        challenge, code, delivered = _issue(
            purpose=OtpPurpose.LOGIN,
            channel=Channel.PHONE,
            address=phone,
            user=user,
            subject_id=None,
            send=send,
        )
        audit.record(
            "auth.otp.requested",
            actor_id=user.pk if user else None,
            school_id=None,
            target_type="otp_challenge",
            target_id=challenge.id,
            metadata={"account_found": user is not None, "delivered": delivered if user else None},
        )
    return _result(challenge, code, echo=user is not None)


def verify_code(challenge_id: str, code: str) -> User:
    """Return the user for a correct, unexpired, unused sign-in code. Any failure is the same ``InvalidOtp``.

    A wrong guess is counted (and committed) before the error is raised.
    """
    with transaction.atomic():
        challenge, reason = _consume(challenge_id, code, purpose=OtpPurpose.LOGIN, subject_id=None)
        user = challenge.user if challenge is not None and reason is None else None
        if reason is None and (user is None or not user.is_active):
            user, reason = None, "no_active_account"
        audit.record(
            "auth.otp.verified",
            outcome=Outcome.SUCCESS if user else Outcome.FAILURE,
            actor_id=challenge.user_id if challenge else None,
            school_id=None,
            target_type="otp_challenge",
            target_id=challenge_id if challenge else "",
            metadata={"reason": reason} if reason else None,
        )
    if user is None:
        raise InvalidOtp()
    return user


# ------------------------------------------------------------------------------------------------ invitations
def request_invitation_code(*, invitation_id: uuid.UUID, channel: str, address: str) -> OtpRequestResult:
    """Send a code to the invitation's recipient, proving control of exactly that channel and address.

    Runs in the caller's transaction. Raises ``ServiceUnavailable`` if the channel cannot deliver; the
    caller's transaction then rolls back, so no challenge exists for an undelivered code.
    """
    if not channel_enabled(channel):
        raise ServiceUnavailable()

    def send(code: str) -> bool:
        message = f"{code} is your EduFlow invitation code. It expires in {_ttl_minutes()} minutes."
        try:
            deliver(channel, address, subject="Your EduFlow invitation code", body=message)
        except DeliveryUnavailable:
            raise ServiceUnavailable() from None
        return True

    challenge, code, _ = _issue(
        purpose=OtpPurpose.INVITATION,
        channel=channel,
        address=address,
        user=None,
        subject_id=invitation_id,
        send=send,
    )
    return _result(challenge, code, echo=True)


def consume_invitation_code(*, challenge_id: str, code: str, invitation_id: uuid.UUID) -> bool:
    """Use up a correct invitation code. Wrong guesses count against the challenge.

    Runs in the caller's transaction; the caller commits the attempt count even when acceptance fails.
    """
    _, reason = _consume(challenge_id, code, purpose=OtpPurpose.INVITATION, subject_id=invitation_id)
    return reason is None
