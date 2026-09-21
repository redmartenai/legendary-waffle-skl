"""
EduFlow backend settings.

Every environment-specific value comes from environment variables (optionally
loaded from backend/.env). Defaults are safe for local development only.
"""

import os
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlparse

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader so local development needs no extra dependency."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv(BASE_DIR / ".env")


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_list(name: str, default: str = "") -> list[str]:
    return [item.strip() for item in env(name, default).split(",") if item.strip()]


SECRET_KEY = env("DJANGO_SECRET_KEY", "dev-insecure-change-me-in-production")
DEBUG = env_bool("DJANGO_DEBUG", True)
ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", "*" if DEBUG else "")
CSRF_TRUSTED_ORIGINS = env_list("DJANGO_CSRF_TRUSTED_ORIGINS")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "corsheaders",
    "drf_spectacular",
    # EduFlow modules. Each one owns its models, services and API.
    "apps.core",
    "apps.tenancy",
    "apps.accounts",
    "apps.academics",
    "apps.attendance",
    "apps.homework",
    "apps.fees",
    "apps.results",
    "apps.announcements",
    "apps.transport",
    "apps.messaging",
    "apps.notifications",
    "apps.realtime",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    # Django admin is for platform staff only; it runs outside the per-school scope.
    "apps.core.middleware.AdminUnscopedMiddleware",
]

ROOT_URLCONF = "eduflow.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "eduflow.wsgi.application"
ASGI_APPLICATION = "eduflow.asgi.application"


def _database_from_url(url: str) -> dict:
    parsed = urlparse(url)
    if parsed.scheme not in {"postgres", "postgresql"}:
        raise ValueError("DATABASE_URL must be a postgres:// URL")
    return {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": parsed.path.lstrip("/"),
        "USER": parsed.username or "",
        "PASSWORD": parsed.password or "",
        "HOST": parsed.hostname or "localhost",
        "PORT": str(parsed.port or 5432),
        "CONN_MAX_AGE": 60,
    }


# SQLite is for local development only. The API server, trip simulator and monitor write at the
# same time, so readers use WAL and writers queue for the lock (up to 20 s) instead of failing
# with "database is locked".
_SQLITE = {
    "ENGINE": "django.db.backends.sqlite3",
    "NAME": BASE_DIR / "db.sqlite3",
    "OPTIONS": {
        "timeout": 20,
        "transaction_mode": "IMMEDIATE",
        "init_command": "PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;",
    },
}

DATABASES = {"default": _database_from_url(env("DATABASE_URL")) if env("DATABASE_URL") else _SQLITE}

AUTH_USER_MODEL = "accounts.User"
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
]

LANGUAGE_CODE = "en"
TIME_ZONE = "Asia/Kolkata"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
MEDIA_URL = "/media/"
MEDIA_ROOT = Path(env("MEDIA_ROOT", str(BASE_DIR / "media")))
DATA_UPLOAD_MAX_MEMORY_SIZE = 12 * 1024 * 1024

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework_simplejwt.authentication.JWTAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "EXCEPTION_HANDLER": "apps.core.exceptions.api_exception_handler",
    "DEFAULT_THROTTLE_RATES": {
        "anon": "120/min",
        "otp_request": "10/hour",
        "otp_verify": "30/hour",
        "ingest": "6000/min",
    },
}

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=int(env("JWT_ACCESS_MINUTES", "30"))),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=int(env("JWT_REFRESH_DAYS", "30"))),
    "ROTATE_REFRESH_TOKENS": True,
    "USER_ID_FIELD": "id",
    "USER_ID_CLAIM": "user_id",
    "AUTH_HEADER_TYPES": ("Bearer",),
}

CORS_ALLOW_ALL_ORIGINS = DEBUG
CORS_ALLOWED_ORIGINS = env_list("CORS_ALLOWED_ORIGINS")
CORS_ALLOW_HEADERS = (
    "accept",
    "authorization",
    "content-type",
    "user-agent",
    "x-requested-with",
    "x-school-id",
    "idempotency-key",
)

# Background work (push delivery, realtime publishing). The immediate backend runs
# tasks inline; production swaps in a queue-backed backend with no code changes.
TASKS = {
    "default": {
        "BACKEND": env("TASKS_BACKEND", "django.tasks.backends.immediate.ImmediateBackend"),
    }
}

EDUFLOW = {
    # Return the OTP in the API response. Local development only.
    "OTP_DEV_ECHO": env_bool("OTP_DEV_ECHO", DEBUG),
    "OTP_TTL_SECONDS": int(env("OTP_TTL_SECONDS", "300")),
    "OTP_MAX_ATTEMPTS": 5,
    "OTP_MAX_PER_HOUR": int(env("OTP_MAX_PER_HOUR", "6")),
    "PUSH_ENABLED": env_bool("EXPO_PUSH_ENABLED", False),
    "EXPO_ACCESS_TOKEN": env("EXPO_ACCESS_TOKEN"),
    "TRACCAR_SHARED_SECRET": env("TRACCAR_SHARED_SECRET", "dev-traccar-secret"),
    "DEFAULT_QUIET_HOURS": ("21:00", "07:00"),
}

# Centrifugo realtime server. When unset, clients fall back to HTTP polling.
# (REALTIME_* rather than CENTRIFUGO_* so Centrifugo doesn't read them as its own settings.)
CENTRIFUGO = {
    "API_URL": env("REALTIME_API_URL"),
    "API_KEY": env("REALTIME_API_KEY"),
    "TOKEN_HMAC_SECRET": env("REALTIME_TOKEN_SECRET"),
    "WS_URL": env("REALTIME_WS_URL"),
}

SPECTACULAR_SETTINGS = {
    "TITLE": "EduFlow API",
    "DESCRIPTION": "School and college platform API (web, mobile and driver apps).",
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
}

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"plain": {"format": "%(asctime)s %(levelname)s %(name)s: %(message)s"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "plain"}},
    "root": {"handlers": ["console"], "level": env("LOG_LEVEL", "INFO")},
    "loggers": {"django.db.backends": {"level": "WARNING"}},
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

if not DEBUG:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
