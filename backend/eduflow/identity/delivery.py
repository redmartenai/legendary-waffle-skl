"""Message delivery to a person's phone or email (docs/security/otp.md#delivery).

The single boundary for outbound messages that carry a code or a link. SMS goes through the configured
``SmsProvider`` (``OTP_SMS_PROVIDER``); email goes through Django's email framework (``EMAIL_BACKEND``).

A channel with no configured provider is *disabled*, and callers check :func:`channel_enabled` before doing
any work, so nothing ever reports a delivery that did not happen. Any failure while sending raises
:class:`DeliveryUnavailable`; the message text (which contains a secret) is never logged here.

========================================  ==================================================================
Email backend                             Use
========================================  ==================================================================
``DisabledEmailBackend`` (default)        Email delivery is off until a real backend is configured
``console.EmailBackend``                  Local development: prints the message to the backend log
``locmem.EmailBackend``                   Tests: ``django.core.mail.outbox``
SMTP or a provider's backend              Production (credentials from the environment)
========================================  ==================================================================
"""

from __future__ import annotations

import smtplib
from collections.abc import Sequence

from django.conf import settings
from django.core.mail import EmailMessage, send_mail
from django.core.mail.backends.base import BaseEmailBackend
from django.db import models

from eduflow.core.logging import get_logger

from .otp.providers import SmsUnavailable, get_sms_provider

log = get_logger(__name__)


class Channel(models.TextChoices):
    EMAIL = "email", "Email"
    PHONE = "phone", "Mobile number"


class DeliveryUnavailable(Exception):
    """The channel is not configured, or the provider failed to accept the message."""


class DisabledEmailBackend(BaseEmailBackend):
    """The default: refuses to send, so email-based flows report "unavailable" instead of pretending."""

    enabled = False

    def send_messages(self, email_messages: Sequence[EmailMessage]) -> int:
        raise DeliveryUnavailable("No email backend is configured (EMAIL_BACKEND).")


def channel_enabled(channel: str) -> bool:
    if channel == Channel.PHONE:
        return bool(getattr(get_sms_provider(), "enabled", True))
    return str(settings.EMAIL_BACKEND) != f"{__name__}.DisabledEmailBackend"


def deliver(channel: str, address: str, *, subject: str, body: str) -> None:
    """Send ``body`` to ``address``. Raises :class:`DeliveryUnavailable` if it could not be handed over."""
    if not channel_enabled(channel):
        raise DeliveryUnavailable(f"The {channel} channel is not configured.")
    try:
        if channel == Channel.PHONE:
            get_sms_provider().send(address, body)
        else:
            send_mail(subject, body, settings.DEFAULT_FROM_EMAIL, [address], fail_silently=False)
    except (SmsUnavailable, smtplib.SMTPException, OSError) as exc:
        log.error("message_delivery_failed", channel=channel, error_type=type(exc).__name__)
        raise DeliveryUnavailable(str(exc)) from None


def mask_address(channel: str, address: str) -> str:
    """A hint safe to show back: ``+91*******78`` or ``as***@example.com``."""
    if channel == Channel.PHONE:
        return address[:3] + "*" * max(0, len(address) - 5) + address[-2:]
    local, _, domain = address.partition("@")
    return f"{local[:2]}***@{domain}"
