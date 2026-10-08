"""Settings for the test suite. Tests always run against real PostgreSQL and Redis (ADR-013)."""

from .base import *  # noqa: F403
from .base import env

DEBUG = False
ALLOWED_HOSTS = ["testserver", "localhost"]
API_DOCS_ENABLED = True  # so the schema endpoints themselves are covered by tests

# Hashing speed only matters to tests, and tests never handle real passwords.
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

# Object storage is exercised in the Docker/CI integration checks. Unit tests stub it.
STORAGE_HEALTHCHECK_ENABLED = env.bool("STORAGE_HEALTHCHECK_ENABLED", default=False)

# A test-only app with toy Section/Student models, used to exercise data scopes before the real domain
# modules exist (eduflow/authz/tests/scopeapp).
INSTALLED_APPS = [*INSTALLED_APPS, "eduflow.authz.tests.scopeapp"]  # noqa: F405

# Tests read sent codes from MemorySmsProvider.outbox; nothing is ever sent.
OTP_SMS_PROVIDER = "eduflow.identity.otp.providers.MemorySmsProvider"
OTP_ECHO_DEV_CODE = False
RATE_LIMITS_ENABLED = True

CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True

# Tests assert on the real rendered log lines, so the format is fixed to JSON regardless of .env.
from eduflow.core.logging import build_logging_config  # noqa: E402

LOG_FORMAT = "json"
LOGGING = build_logging_config(level="INFO", fmt="json")
