"""Rate limits for authentication endpoints (ADR-016, docs/security/authentication.md#rate-limits).

Built on DRF's sliding-window throttle, stored in Redis (the Django cache). Differences from DRF's defaults:

* Rates are read from ``settings.RATE_LIMITS`` on every request, so tests and environments can change them,
  and accept multi-unit windows such as ``"5/15m"``.
* The client IP comes from ``eduflow.core.client_ip``, which trusts ``X-Forwarded-For`` only for the
  configured number of proxies.
* Identifiers (email, phone) are hashed before they become cache keys, so Redis never holds them in clear.
* ``RATE_LIMITS_ENABLED=false`` turns them off for local experiments; production refuses to start that way.

A blocked request gets ``429 rate_limited`` with ``retry_after_seconds``.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from django.conf import settings
from rest_framework.request import Request
from rest_framework.throttling import SimpleRateThrottle
from rest_framework.views import APIView

from eduflow.core.client_ip import client_ip

from .phone import InvalidPhone, normalize_phone

_RATE = re.compile(r"^(\d+)/(\d*)([smhd])$")
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_rate(rate: str) -> tuple[int, int]:
    match = _RATE.fullmatch(rate.strip())
    if not match:
        raise ValueError(f"Invalid rate {rate!r}; expected e.g. '5/m' or '5/15m'.")
    count, multiplier, unit = match.groups()
    return int(count), int(multiplier or 1) * _UNIT_SECONDS[unit]


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:32]


class EduFlowThrottle(SimpleRateThrottle):
    scope: str = ""
    cache_format = "throttle:%(scope)s:%(ident)s"

    def __init__(self) -> None:  # DRF reads the rate here; ours is read per request in allow_request.
        pass

    def get_rate(self) -> str | None:
        return str(settings.RATE_LIMITS[self.scope])

    def ident_for(self, request: Request) -> str | None:
        raise NotImplementedError

    def get_cache_key(self, request: Request, view: APIView) -> str | None:
        ident = self.ident_for(request)
        if not ident:
            return None
        return self.cache_format % {"scope": self.scope, "ident": ident}

    def allow_request(self, request: Request, view: APIView) -> bool:
        if not settings.RATE_LIMITS_ENABLED:
            return True
        self.rate = self.get_rate()
        self.num_requests, self.duration = parse_rate(self.rate or "")
        return super().allow_request(request, view)


class IpThrottle(EduFlowThrottle):
    def ident_for(self, request: Request) -> str | None:
        return client_ip(request) or "unknown"


class BodyFieldThrottle(EduFlowThrottle):
    """Keyed on a request-body field (e.g. the sign-in identifier), so one account is limited across IPs."""

    field = ""

    def normalize(self, value: str) -> str:
        return value.strip().lower()

    def ident_for(self, request: Request) -> str | None:
        data: Any = request.data
        value = data.get(self.field) if hasattr(data, "get") else None
        # Serializer fields accept numbers too ({"phone": 9812345678}), so must the throttle; otherwise a
        # JSON number would skip the per-account limit.
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            value = str(value)
        if not isinstance(value, str) or not value.strip():
            return None
        return _digest(self.normalize(value))


class PhoneFieldThrottle(BodyFieldThrottle):
    field = "phone"

    def normalize(self, value: str) -> str:
        try:
            return normalize_phone(value)
        except InvalidPhone:
            return value.strip()


class UserThrottle(EduFlowThrottle):
    def ident_for(self, request: Request) -> str | None:
        user = request.user
        return str(user.pk) if user is not None and user.is_authenticated else None


class LoginIpThrottle(IpThrottle):
    scope = "login_ip"


class LoginIdentifierThrottle(BodyFieldThrottle):
    scope = "login_identifier"
    field = "identifier"

    def normalize(self, value: str) -> str:
        value = value.strip().lower()
        if "@" in value:
            return value
        try:
            return normalize_phone(value)
        except InvalidPhone:
            return value


class RefreshIpThrottle(IpThrottle):
    scope = "refresh_ip"


class OtpRequestIpThrottle(IpThrottle):
    scope = "otp_request_ip"


class OtpRequestPhoneThrottle(PhoneFieldThrottle):
    scope = "otp_request_phone"


class OtpVerifyIpThrottle(IpThrottle):
    scope = "otp_verify_ip"


class OtpVerifyChallengeThrottle(BodyFieldThrottle):
    scope = "otp_verify_challenge"
    field = "challenge_id"


class PasswordChangeUserThrottle(UserThrottle):
    scope = "password_change_user"


class SchoolLookupIpThrottle(IpThrottle):
    scope = "school_lookup_ip"


class MemberCreateUserThrottle(UserThrottle):
    scope = "member_create_user"


class InvitationManageUserThrottle(UserThrottle):
    scope = "invitation_manage_user"


class InvitationIpThrottle(IpThrottle):
    scope = "invitation_ip"


class InvitationTokenThrottle(BodyFieldThrottle):
    """Per invitation secret (hashed), so one link cannot be hammered from many IPs."""

    scope = "invitation_token"
    field = "token"
