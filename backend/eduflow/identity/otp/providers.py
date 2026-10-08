"""SMS provider adapters for one-time codes (docs/security/otp.md).

The OTP service talks only to :class:`SmsProvider`. The adapter is chosen by ``OTP_SMS_PROVIDER`` (a dotted
path), so a production gateway (MSG91, Twilio, ...) is one new class plus configuration, with no change to the
OTP rules. Its credentials come from the environment, never from code.

=========================  =========================================================================
Adapter                    Use
=========================  =========================================================================
``DisabledSmsProvider``    Default. Refuses to send, so OTP sign-in is off until a gateway is set.
``ConsoleSmsProvider``     Local development. Writes the message to the log. Refuses to run unless
                           ``DEBUG`` is on, and production settings refuse to start with it.
``MemorySmsProvider``      Tests. Keeps messages in ``MemorySmsProvider.outbox``; nothing is sent.
=========================  =========================================================================
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar, Protocol

from django.conf import settings
from django.utils.module_loading import import_string

from eduflow.core.logging import get_logger

from ..phone import mask_phone

log = get_logger(__name__)


class SmsUnavailable(Exception):
    """The provider cannot send right now (or is not configured)."""


class SmsProvider(Protocol):
    def send(self, phone: str, message: str) -> None: ...


class DisabledSmsProvider:
    # The OTP service refuses every request up front (503) when the provider is not enabled, so the
    # response never depends on whether the number has an account.
    enabled = False

    def send(self, phone: str, message: str) -> None:
        raise SmsUnavailable("No SMS provider is configured (OTP_SMS_PROVIDER).")


class ConsoleSmsProvider:
    def send(self, phone: str, message: str) -> None:
        if not settings.DEBUG:
            raise SmsUnavailable("The console SMS provider only works with DEBUG on.")
        # Development only: the message (which contains the code) is printed so a developer can sign in.
        log.warning("dev_sms", to=mask_phone(phone), body=message)


@dataclass(frozen=True)
class SentSms:
    phone: str
    message: str


class MemorySmsProvider:
    outbox: ClassVar[list[SentSms]] = []

    def send(self, phone: str, message: str) -> None:
        self.outbox.append(SentSms(phone, message))


def get_sms_provider() -> SmsProvider:
    provider: SmsProvider = import_string(settings.OTP_SMS_PROVIDER)()
    return provider
