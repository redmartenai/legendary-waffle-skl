"""One-time-code sign-in (docs/security/otp.md)."""

import re
from datetime import timedelta

import pytest
import time_machine
from django.utils import timezone

from eduflow.audit.models import AuditEvent
from eduflow.identity.models import OtpChallenge
from eduflow.identity.otp.providers import ConsoleSmsProvider, MemorySmsProvider, SmsUnavailable
from eduflow.identity.otp.service import code_digest

pytestmark = pytest.mark.django_db

PHONE = "+919812345678"
REQUEST = "/api/v1/auth/otp/request"
VERIFY = "/api/v1/auth/otp/verify"


@pytest.fixture
def user(make_user):
    return make_user(phone=PHONE, password=None)


def _request(client, phone=PHONE, **extra):
    return client.post(REQUEST, {"phone": phone}, format="json", **extra)


def _verify(client, challenge_id, code, **extra):
    return client.post(VERIFY, {"challenge_id": challenge_id, "code": code}, format="json", **extra)


def _sent_code():
    match = re.search(r"\b(\d{6})\b", MemorySmsProvider.outbox[-1].message)
    assert match is not None
    return match.group(1)


def _wrong(code):
    return f"{(int(code) + 1) % 1_000_000:06d}"


def test_request_and_verify_signs_in(api_client, user):
    requested = _request(api_client)
    assert requested.status_code == 200, requested.content
    body = requested.json()
    assert body["expires_in"] == 300
    assert body["resend_in"] == 30
    assert "dev_code" not in body
    assert MemorySmsProvider.outbox[-1].phone == PHONE

    response = _verify(api_client, body["challenge_id"], _sent_code())

    assert response.status_code == 200
    assert response.json()["user"]["id"] == str(user.id)
    assert response.json()["refresh"]


def test_national_number_is_normalised(api_client, user):
    assert _request(api_client, "98123 45678").status_code == 200
    assert MemorySmsProvider.outbox[-1].phone == PHONE


def test_code_is_stored_only_as_hmac(api_client, user):
    challenge_id = _request(api_client).json()["challenge_id"]
    code = _sent_code()
    row = OtpChallenge.objects.get(pk=challenge_id)
    assert row.code_hash == code_digest(challenge_id, code)
    assert code not in row.code_hash
    assert PHONE not in row.address_hash


def test_code_is_single_use(api_client, user):
    challenge_id = _request(api_client).json()["challenge_id"]
    code = _sent_code()
    assert _verify(api_client, challenge_id, code).status_code == 200
    replay = _verify(api_client, challenge_id, code)
    assert replay.status_code == 401
    assert replay.json()["error"]["code"] == "invalid_code"


def test_expired_code_is_rejected(api_client, user):
    challenge_id = _request(api_client).json()["challenge_id"]
    code = _sent_code()
    with time_machine.travel(timezone.now() + timedelta(seconds=301)):
        assert _verify(api_client, challenge_id, code).status_code == 401


def test_invalid_code_is_rejected_and_counted(api_client, user):
    challenge_id = _request(api_client).json()["challenge_id"]
    response = _verify(api_client, challenge_id, _wrong(_sent_code()))
    assert response.status_code == 401
    assert response.json()["error"] == {
        "code": "invalid_code",
        "message": "That code is incorrect or has expired.",
        "request_id": response["X-Request-ID"],
    }
    assert OtpChallenge.objects.get(pk=challenge_id).attempts == 1


def test_attempt_limit_locks_the_challenge(api_client, user):
    challenge_id = _request(api_client).json()["challenge_id"]
    code = _sent_code()
    for _ in range(5):
        assert _verify(api_client, challenge_id, _wrong(code)).status_code == 401
    # Even the right code no longer works.
    assert _verify(api_client, challenge_id, code).status_code == 401
    row = OtpChallenge.objects.get(pk=challenge_id)
    assert row.attempts == 5
    assert row.invalidated_at is not None


def test_unknown_number_gets_identical_response_and_no_sms(api_client, db):
    response = _request(api_client, "+919800000000")
    assert response.status_code == 200
    assert set(response.json()) == {"challenge_id", "expires_in", "resend_in"}
    assert MemorySmsProvider.outbox == []
    # Guessing can never sign anyone in through such a challenge.
    challenge_id = response.json()["challenge_id"]
    assert all(_verify(api_client, challenge_id, f"{n:06d}").status_code == 401 for n in range(3))


def test_inactive_account_gets_no_sms(api_client, user):
    user.is_active = False
    user.save()
    assert _request(api_client).status_code == 200
    assert MemorySmsProvider.outbox == []


def test_resend_cooldown(api_client, user):
    first = _request(api_client).json()["challenge_id"]
    too_soon = _request(api_client)
    assert too_soon.status_code == 429
    assert 1 <= too_soon.json()["error"]["retry_after_seconds"] <= 30

    with time_machine.travel(timezone.now() + timedelta(seconds=31)):
        second = _request(api_client).json()["challenge_id"]
        # A new code invalidates the previous challenge.
        assert OtpChallenge.objects.get(pk=first).invalidated_at is not None
        assert second != first


def test_new_code_invalidates_older_code(api_client, user):
    first = _request(api_client).json()["challenge_id"]
    first_code = _sent_code()
    with time_machine.travel(timezone.now() + timedelta(seconds=31)):
        _request(api_client)
        assert _verify(api_client, first, first_code).status_code == 401


def test_invalid_phone_is_a_validation_error(api_client, db):
    response = _request(api_client, "12")
    assert response.status_code == 400
    assert "phone" in response.json()["error"]["fields"]


def test_unknown_challenge_id(api_client, db):
    assert _verify(api_client, "01900000-0000-7000-8000-000000000000", "123456").status_code == 401


def test_sms_provider_unavailable_is_503_and_creates_nothing(api_client, user, settings):
    settings.OTP_SMS_PROVIDER = "eduflow.identity.otp.providers.DisabledSmsProvider"
    response = _request(api_client)
    assert response.status_code == 503
    assert not OtpChallenge.objects.exists()


def test_dev_code_only_with_debug_and_flag(api_client, user, settings):
    settings.OTP_ECHO_DEV_CODE = True
    settings.DEBUG = False
    assert "dev_code" not in _request(api_client).json()

    settings.DEBUG = True
    with time_machine.travel(timezone.now() + timedelta(seconds=31)):
        body = _request(api_client).json()
    assert body["dev_code"] == _sent_code()


def test_console_provider_refuses_without_debug(settings):
    settings.DEBUG = False
    with pytest.raises(SmsUnavailable):
        ConsoleSmsProvider().send(PHONE, "123456 is your code")


def test_otp_never_appears_in_logs_or_audit(api_client, user, json_logs):
    challenge_id = _request(api_client).json()["challenge_id"]
    code = _sent_code()
    _verify(api_client, challenge_id, _wrong(code))
    _verify(api_client, challenge_id, code)

    assert code not in json_logs.text
    assert PHONE not in json_logs.text
    for event in AuditEvent.objects.all():
        assert code not in str(event.metadata)
        assert PHONE not in str(event.metadata)
    actions = list(AuditEvent.objects.order_by("id").values_list("action", "outcome"))
    assert actions == [
        ("auth.otp.requested", "success"),
        ("auth.otp.verified", "failure"),
        ("auth.otp.verified", "success"),
        ("identity.phone.verified", "success"),
        ("auth.login", "success"),
    ]
