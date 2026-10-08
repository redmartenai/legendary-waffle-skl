"""Settings shared by every environment.

All environment-specific values come from environment variables (ADR-014). Nothing here may
contain a real secret. Environment modules (dev/test/prod) import this and override.
"""

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
    "rest_framework",
    "drf_spectacular",
    "eduflow.core",
]

MIDDLEWARE = [
    "eduflow.core.middleware.RequestContextMiddleware",
    "django.middleware.security.SecurityMiddleware",
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

# Feature flags that must never be on in production (enforced by config_validation).
OTP_ECHO_DEV_CODE = env.bool("OTP_ECHO_DEV_CODE", default=False)
API_DOCS_ENABLED = env.bool("API_DOCS_ENABLED", default=False)

# ----------------------------------------------------------------------------- DRF
REST_FRAMEWORK = {
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    "DEFAULT_PARSER_CLASSES": [
        "rest_framework.parsers.JSONParser",
        "rest_framework.parsers.MultiPartParser",
    ],
    # Authentication arrives in Phase 2. Until then nothing authenticates and the
    # default permission denies, so an endpoint is only public if it opts in explicitly.
    "DEFAULT_AUTHENTICATION_CLASSES": [],
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
    "core.heartbeat": {"task": "eduflow.core.tasks.heartbeat", "schedule": 300.0},
}

# ----------------------------------------------------------------------------- logging
LOG_LEVEL = env.str("LOG_LEVEL", default="INFO")
LOG_FORMAT = env.str("LOG_FORMAT", default="json")  # "json" | "console"

from eduflow.core.logging import build_logging_config  # noqa: E402

LOGGING = build_logging_config(level=LOG_LEVEL, fmt=LOG_FORMAT)
