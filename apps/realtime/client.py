"""Centrifugo integration: tokens for clients and server-side publishing.

Django decides who may see what; Centrifugo only fans messages out. Live
tracking and chat therefore keep working across Django deploys, and clients
fall back to HTTP polling whenever the realtime server is unavailable.
"""

import logging
import time

import jwt
from django.conf import settings
from django.db import transaction

logger = logging.getLogger("apps.realtime")


def _config() -> dict:
    return settings.CENTRIFUGO


def enabled() -> bool:
    config = _config()
    return bool(config["API_URL"] and config["API_KEY"] and config["TOKEN_HMAC_SECRET"])


def personal_channel(user_id) -> str:
    # User-limited channel: Centrifugo only lets the connection whose user id
    # matches the part after "#" subscribe, so no subscription token is needed.
    return f"personal:#{user_id}"


def trip_channel(trip_id) -> str:
    return f"trip:{trip_id}"


def connection_token(user, ttl_seconds: int = 3600) -> str:
    payload = {"sub": str(user.id), "exp": int(time.time()) + ttl_seconds}
    return jwt.encode(payload, _config()["TOKEN_HMAC_SECRET"], algorithm="HS256")


def subscription_token(user, channel: str, ttl_seconds: int) -> str:
    payload = {"sub": str(user.id), "channel": channel, "exp": int(time.time()) + ttl_seconds}
    return jwt.encode(payload, _config()["TOKEN_HMAC_SECRET"], algorithm="HS256")


def publish(channel: str, data: dict) -> None:
    """Publish after the surrounding transaction commits (never inside it)."""
    if not enabled():
        return

    def _enqueue():
        from .tasks import publish_to_centrifugo

        publish_to_centrifugo.enqueue(channel, data)

    transaction.on_commit(_enqueue)
