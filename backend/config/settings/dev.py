"""Local development. Never used for a deployed environment."""

from .base import *  # noqa: F403
from .base import env

DEBUG = env.bool("DJANGO_DEBUG", default=True)
ALLOWED_HOSTS = env.list("DJANGO_ALLOWED_HOSTS", default=["localhost", "127.0.0.1", "[::1]", "backend"])
API_DOCS_ENABLED = env.bool("API_DOCS_ENABLED", default=True)
