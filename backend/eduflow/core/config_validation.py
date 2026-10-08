"""Fail-fast validation of production settings (ADR-014).

``config.settings.prod`` calls :func:`validate_production_settings` at import time, so an insecure
production deployment refuses to start instead of running quietly with a dangerous setting.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from django.core.exceptions import ImproperlyConfigured

_WEAK_KEY_MARKERS = ("insecure", "change-me", "changeme", "dev-only", "secret", "example", "test")
MIN_SECRET_KEY_LENGTH = 50
MIN_SECRET_KEY_UNIQUE_CHARS = 5


def production_problems(settings: Mapping[str, Any]) -> list[str]:
    problems: list[str] = []

    if settings.get("DEBUG"):
        problems.append("DJANGO_DEBUG must be false in production.")

    key = str(settings.get("SECRET_KEY") or "")
    if (
        len(key) < MIN_SECRET_KEY_LENGTH
        or len(set(key)) < MIN_SECRET_KEY_UNIQUE_CHARS
        or any(m in key.lower() for m in _WEAK_KEY_MARKERS)
    ):
        problems.append(
            f"DJANGO_SECRET_KEY must be a random value of at least {MIN_SECRET_KEY_LENGTH} characters "
            "and must not be a placeholder."
        )

    hosts = list(settings.get("ALLOWED_HOSTS") or [])
    if not hosts:
        problems.append("DJANGO_ALLOWED_HOSTS must list the production host names.")
    elif any(h in ("*", ".*") or h.startswith("*") for h in hosts):
        problems.append("DJANGO_ALLOWED_HOSTS must not contain wildcards.")

    if settings.get("OTP_ECHO_DEV_CODE"):
        problems.append("OTP_ECHO_DEV_CODE must be false in production (it would bypass OTP).")

    if settings.get("API_DOCS_ENABLED"):
        problems.append("API_DOCS_ENABLED must be false in production.")

    if not settings.get("SESSION_COOKIE_SECURE") or not settings.get("CSRF_COOKIE_SECURE"):
        problems.append("Session and CSRF cookies must be Secure in production.")

    if not settings.get("SECURE_SSL_REDIRECT") and not settings.get("SECURE_PROXY_SSL_HEADER"):
        problems.append("Production must redirect to HTTPS or trust a TLS-terminating proxy header.")

    db = (settings.get("DATABASES") or {}).get("default", {})
    if db.get("PASSWORD") in (None, "", "postgres", "eduflow", "password"):
        problems.append("The production database password is missing or a well-known default.")

    return problems


def validate_production_settings(settings: Mapping[str, Any]) -> None:
    problems = production_problems(settings)
    if problems:
        raise ImproperlyConfigured("Insecure production configuration:\n- " + "\n- ".join(problems))
