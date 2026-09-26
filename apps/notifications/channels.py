"""SMS, WhatsApp and email delivery behind one small interface.

Until the school picks providers (and adds credentials), every channel uses the logging provider: the message is
recorded as a ChannelDelivery with status "logged" and written to the log, so nothing is silently dropped and the
reach numbers stay honest. A real provider only has to implement `send`.
"""

import logging
import uuid

from django.conf import settings
from django.utils.module_loading import import_string

log = logging.getLogger("eduflow.channels")


class LoggingProvider:
    name = "log"
    delivered_status = "logged"

    def send(self, channel: str, address: str, text: str) -> str:
        ref = f"log-{uuid.uuid4().hex[:12]}"
        log.info("%s to %s (%s): %s", channel, address, ref, text[:160])
        return ref


def provider_for(channel: str):
    path = (getattr(settings, "EDUFLOW_CHANNEL_PROVIDERS", {}) or {}).get(channel)
    return import_string(path)() if path else LoggingProvider()


def address_for(channel: str, user) -> str | None:
    if channel in ("sms", "whatsapp"):
        return user.phone or None
    if channel == "email":
        return user.email or None
    return None
