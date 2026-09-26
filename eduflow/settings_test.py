"""Test settings: always local SQLite and local files, whatever ``.env`` says.

``.env`` may point at Supabase (Postgres + Storage) for running the server; tests must never touch it.
To check the code against Postgres on purpose, run pytest with ``DJANGO_SETTINGS_MODULE=eduflow.settings``.
"""

from .settings import *  # noqa: F403
from .settings import _SQLITE, BASE_DIR

DATABASE_SCHEMA = ""
DATABASES = {"default": _SQLITE}
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}
MEDIA_ROOT = BASE_DIR / "media-test"
