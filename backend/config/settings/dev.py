"""Local development. Never used for a deployed environment."""

from .base import *  # noqa: F403
from .base import env

DEBUG = env.bool("DJANGO_DEBUG", default=True)
ALLOWED_HOSTS = env.list("DJANGO_ALLOWED_HOSTS", default=["localhost", "127.0.0.1", "[::1]", "backend"])
API_DOCS_ENABLED = env.bool("API_DOCS_ENABLED", default=True)

# One-time codes are written to the log instead of being sent (eduflow.identity.otp.providers).
OTP_SMS_PROVIDER = env.str("OTP_SMS_PROVIDER", default="eduflow.identity.otp.providers.ConsoleSmsProvider")
# Invitation emails are printed to the backend log instead of being sent.
EMAIL_BACKEND = env.str("EMAIL_BACKEND", default="django.core.mail.backends.console.EmailBackend")
