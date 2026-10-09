"""Settings shared by every environment.

All environment-specific values come from environment variables (ADR-014). Nothing here may
contain a real secret. Environment modules (dev/test/prod) import this and override.
"""

import hashlib
from datetime import timedelta
from pathlib import Path

import environ

BASE_DIR = Path(__file__).resolve().parent.parent.parent  # backend/
REPO_DIR = BASE_DIR.parent

env = environ.Env()

# Local, non-Docker runs may keep variables in the repo-root .env (never committed). Containers get
# their environment from Compose or the platform, and the image never contains a .env file.
if env.bool("DJANGO_READ_DOT_ENV", default=True) and (REPO_DIR / ".env").is_file():
    env.read_env(str(REPO_DIR / ".env"))

# ----------------------------------------------------------------------------- core
SECRET_KEY = env.str("DJANGO_SECRET_KEY")
DEBUG = env.bool("DJANGO_DEBUG", default=False)
ALLOWED_HOSTS: list[str] = env.list("DJANGO_ALLOWED_HOSTS", default=[])

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "django.contrib.postgres",
    "rest_framework",
    "drf_spectacular",
    "eduflow.core",
    "eduflow.identity",
    "eduflow.tenancy",
    "eduflow.authz",
    "eduflow.audit",
    "eduflow.academics",
    "eduflow.people",
    "eduflow.invitations",
]

MIDDLEWARE = [
    "eduflow.core.middleware.RequestContextMiddleware",
    # Every request (except health probes) runs under the RLS-enforced database role (ADR-018).
    "eduflow.core.middleware.DatabaseContextMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "eduflow.core.middleware.SecurityHeadersMiddleware",
    "django.middleware.common.CommonMiddleware",
    # DRF views using token auth are CSRF-exempt by design. This protects any cookie-authenticated
    # endpoint (the planned web-console session, ADR-005).
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

# The client calls paths without trailing slashes (docs/api/conventions.md).
APPEND_SLASH = False

TEMPLATES: list[dict[str, object]] = []  # JSON API only, no server-rendered pages.

LANGUAGE_CODE = "en"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ----------------------------------------------------------------------------- database
DATABASES = {"default": env.db_url("DATABASE_URL")}
DATABASES["default"]["CONN_MAX_AGE"] = env.int("DATABASE_CONN_MAX_AGE", default=60)
DATABASES["default"]["CONN_HEALTH_CHECKS"] = True
# Transactions are explicit in services (ADR-001), not one per request.
DATABASES["default"]["ATOMIC_REQUESTS"] = False

# ----------------------------------------------------------------------------- cache / redis
REDIS_URL = env.str("REDIS_URL")
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": REDIS_URL,
        "KEY_PREFIX": "eduflow",
        "TIMEOUT": 300,
    }
}

# Requests and tasks switch to this NOLOGIN role so RLS policies apply (core migration 0001, ADR-018).
# Set it empty only when the app already connects as a dedicated non-owner login role.
DATABASE_RLS_ROLE = env.str("DATABASE_RLS_ROLE", default="eduflow_app")

# ----------------------------------------------------------------------------- identity
AUTH_USER_MODEL = "identity.User"
# Phone numbers without a country code are national numbers of this country (the client assumes India).
PHONE_DEFAULT_COUNTRY_CODE = env.str("PHONE_DEFAULT_COUNTRY_CODE", default="91")
PHONE_NATIONAL_NUMBER_LENGTH = env.int("PHONE_NATIONAL_NUMBER_LENGTH", default=10)

# ----------------------------------------------------------------------------- passwords
PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.Argon2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
]
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 10}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# ----------------------------------------------------------------------------- security headers
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
SECURE_CROSS_ORIGIN_OPENER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"
CSRF_FAILURE_VIEW = "eduflow.core.views.csrf_failure"
# Prepared for the web console's future cookie session (docs/security/authentication.md#web-sessions).
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE = "Lax"
# No CORS headers are sent, so browsers refuse cross-origin calls. The API is meant to be same-origin
# behind the reverse proxy; a cross-origin web client needs an explicit allow-list
# (docs/security/authentication.md).

# How many reverse proxies we operate in front of the app. X-Forwarded-For is trusted only that far
# (eduflow.core.client_ip). 0 = ignore the header and use the socket address.
TRUSTED_PROXY_COUNT = env.int("TRUSTED_PROXY_COUNT", default=0)

# Feature flags that must never be on in production (enforced by config_validation).
OTP_ECHO_DEV_CODE = env.bool("OTP_ECHO_DEV_CODE", default=False)
API_DOCS_ENABLED = env.bool("API_DOCS_ENABLED", default=False)

# ----------------------------------------------------------------------------- tokens (ADR-005)
ACCESS_TOKEN_LIFETIME = timedelta(minutes=env.int("ACCESS_TOKEN_MINUTES", default=10))
REFRESH_TOKEN_LIFETIME = timedelta(days=env.int("REFRESH_TOKEN_DAYS", default=1))
REFRESH_TOKEN_REMEMBER_LIFETIME = timedelta(days=env.int("REFRESH_TOKEN_REMEMBER_DAYS", default=30))
# A session ends this long after sign-in, however often it is refreshed.
AUTH_SESSION_MAX_AGE = timedelta(days=env.int("AUTH_SESSION_MAX_DAYS", default=90))
AUTH_RECORD_RETENTION_DAYS = env.int("AUTH_RECORD_RETENTION_DAYS", default=30)
# A separate key lets access tokens be invalidated without rotating SECRET_KEY. By default it is derived from
# SECRET_KEY with a domain label, so the raw SECRET_KEY is never used as an HMAC key for tokens.
JWT_SIGNING_KEY = (
    env.str("JWT_SIGNING_KEY", default="")
    or hashlib.sha256(b"eduflow.jwt-signing:" + SECRET_KEY.encode()).hexdigest()
)
SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": ACCESS_TOKEN_LIFETIME,
    "ALGORITHM": "HS256",
    "SIGNING_KEY": JWT_SIGNING_KEY,
    "AUDIENCE": "eduflow-api",
    "ISSUER": "eduflow",
    "LEEWAY": 0,
    "AUTH_HEADER_TYPES": ("Bearer",),
    "USER_ID_FIELD": "id",
    "USER_ID_CLAIM": "sub",
    "TOKEN_TYPE_CLAIM": "token_type",
    "JTI_CLAIM": "jti",
    "UPDATE_LAST_LOGIN": False,
}

# ----------------------------------------------------------------------------- OTP
# See docs/security/otp.md.
OTP_SMS_PROVIDER = env.str("OTP_SMS_PROVIDER", default="eduflow.identity.otp.providers.DisabledSmsProvider")
OTP_LENGTH = 6
OTP_TTL_SECONDS = env.int("OTP_TTL_SECONDS", default=300)
OTP_MAX_ATTEMPTS = env.int("OTP_MAX_ATTEMPTS", default=5)
OTP_RESEND_SECONDS = env.int("OTP_RESEND_SECONDS", default=30)

# ----------------------------------------------------------------------------- invitations (ADR-025)
# docs/security/invitations.md. The secret is appended to INVITATION_LINK_BASE; put it after "#" so it stays
# in the browser and never reaches server or proxy logs.
INVITATION_TTL_HOURS = env.int("INVITATION_TTL_HOURS", default=72)
INVITATION_RESEND_SECONDS = env.int("INVITATION_RESEND_SECONDS", default=60)
INVITATION_LINK_BASE = env.str("INVITATION_LINK_BASE", default="http://localhost:8081/invite#token=")

# ----------------------------------------------------------------------------- email (delivery adapter)
# Disabled by default, so email-based flows answer "unavailable" until a real backend is configured.
EMAIL_BACKEND = env.str("EMAIL_BACKEND", default="eduflow.identity.delivery.DisabledEmailBackend")
DEFAULT_FROM_EMAIL = env.str("DEFAULT_FROM_EMAIL", default="EduFlow <no-reply@eduflow.invalid>")
EMAIL_HOST = env.str("EMAIL_HOST", default="localhost")
EMAIL_PORT = env.int("EMAIL_PORT", default=587)
EMAIL_HOST_USER = env.str("EMAIL_HOST_USER", default="")
EMAIL_HOST_PASSWORD = env.str("EMAIL_HOST_PASSWORD", default="")
EMAIL_USE_TLS = env.bool("EMAIL_USE_TLS", default=True)
EMAIL_TIMEOUT = 10

# ----------------------------------------------------------------------------- rate limits (ADR-016)
# "<requests>/<window>", window = s|m|h|d with an optional multiplier, e.g. "5/15m".
RATE_LIMITS_ENABLED = env.bool("RATE_LIMITS_ENABLED", default=True)
RATE_LIMITS = {
    "login_ip": "20/5m",
    "login_identifier": "5/5m",
    "refresh_ip": "60/m",
    "otp_request_ip": "10/h",
    "otp_request_phone": "3/10m",
    "otp_verify_ip": "30/10m",
    "otp_verify_challenge": "10/10m",
    "password_change_user": "5/h",
    "school_lookup_ip": "30/m",
    "member_create_user": "60/h",
    "invitation_manage_user": "60/h",
    "invitation_ip": "30/10m",
    "invitation_token": "10/10m",
}

# ----------------------------------------------------------------------------- DRF
REST_FRAMEWORK = {
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    "DEFAULT_PARSER_CLASSES": [
        "rest_framework.parsers.JSONParser",
        "rest_framework.parsers.MultiPartParser",
    ],
    # Deny by default: an endpoint is public only if it opts in explicitly (AllowAny).
    "DEFAULT_AUTHENTICATION_CLASSES": ["eduflow.identity.authentication.AccessTokenAuthentication"],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "UNAUTHENTICATED_USER": None,
    "EXCEPTION_HANDLER": "eduflow.core.exceptions.api_exception_handler",
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DEFAULT_VERSIONING_CLASS": None,
}

SPECTACULAR_SETTINGS = {
    "TITLE": "EduFlow API",
    "DESCRIPTION": "EduFlow School Operating System API. See docs/api/conventions.md.",
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
    "SCHEMA_PATH_PREFIX": r"/api/v1",
    "COMPONENT_SPLIT_REQUEST": True,
    "OAS_VERSION": "3.1.0",
    "ENUM_NAME_OVERRIDES": {
        "DataScopeEnum": "eduflow.authz.catalog.DataScope",
        "RecordStatusEnum": "eduflow.academics.models.RecordStatus",
        "AcademicYearStatusEnum": "eduflow.academics.models.AcademicYearStatus",
        "StaffStatusEnum": "eduflow.people.models.StaffStatus",
        "StudentStatusEnum": "eduflow.people.models.StudentStatus",
        "EnrollmentStatusEnum": "eduflow.people.models.EnrollmentStatus",
        "EnrollmentEndStatusEnum": ["completed", "withdrawn"],
        "AssignmentStatusEnum": "eduflow.people.models.AssignmentStatus",
        "HealthStatusEnum": ["ok", "unavailable"],
    },
}

# ----------------------------------------------------------------------------- object storage
# S3-compatible (ADR-009). Files are private by default. Downloads are authorized by the API and
# then redirected to short-lived signed URLs (from Phase 7).
STORAGE_BUCKET = env.str("STORAGE_BUCKET", default="eduflow-private")
STORAGE_ENDPOINT_URL = env.str("STORAGE_ENDPOINT_URL", default="") or None
STORAGE_REGION = env.str("STORAGE_REGION", default="us-east-1")
STORAGE_ACCESS_KEY = env.str("STORAGE_ACCESS_KEY", default="")
STORAGE_SECRET_KEY = env.str("STORAGE_SECRET_KEY", default="")
STORAGE_HEALTHCHECK_ENABLED = env.bool("STORAGE_HEALTHCHECK_ENABLED", default=True)
STORAGES = {
    "default": {
        "BACKEND": "storages.backends.s3.S3Storage",
        "OPTIONS": {
            "bucket_name": STORAGE_BUCKET,
            "endpoint_url": STORAGE_ENDPOINT_URL,
            "region_name": STORAGE_REGION,
            "access_key": STORAGE_ACCESS_KEY,
            "secret_key": STORAGE_SECRET_KEY,
            "default_acl": None,
            "querystring_auth": True,
            "querystring_expire": 60,
            "file_overwrite": False,
            "addressing_style": "path",
            "signature_version": "s3v4",
        },
    },
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}

# ----------------------------------------------------------------------------- celery
CELERY_BROKER_URL = env.str("CELERY_BROKER_URL", default=REDIS_URL)
CELERY_RESULT_BACKEND = env.str("CELERY_RESULT_BACKEND", default="") or None
CELERY_RESULT_EXPIRES = 3600
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TIMEZONE = "UTC"
CELERY_TASK_DEFAULT_QUEUE = "default"
CELERY_TASK_ACKS_LATE = True  # tasks are retry-safe (ADR-010)
CELERY_TASK_REJECT_ON_WORKER_LOST = True
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_TASK_TIME_LIMIT = 300
CELERY_TASK_SOFT_TIME_LIMIT = 240
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True
CELERY_WORKER_HIJACK_ROOT_LOGGER = False
CELERY_BEAT_SCHEDULE = {
    # Interval schedules first fire one interval after beat starts.
    "core.heartbeat": {
        "task": "eduflow.core.tasks.heartbeat",
        "schedule": env.float("CELERY_HEARTBEAT_SECONDS", default=300.0),
    },
    "invitations.expire_due": {
        "task": "eduflow.invitations.tasks.expire_due_invitations",
        "schedule": 15 * 60.0,
    },
    "identity.purge_expired_auth_records": {
        "task": "eduflow.identity.tasks.purge_expired_auth_records",
        "schedule": 6 * 3600.0,
    },
}

# ----------------------------------------------------------------------------- logging
LOG_LEVEL = env.str("LOG_LEVEL", default="INFO")
LOG_FORMAT = env.str("LOG_FORMAT", default="json")  # "json" | "console"

from eduflow.core.logging import build_logging_config  # noqa: E402

LOGGING = build_logging_config(level=LOG_LEVEL, fmt=LOG_FORMAT)
