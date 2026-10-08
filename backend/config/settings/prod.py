"""Production (and any shared, internet-reachable environment).

Secure by default. The module refuses to load if the configuration is unsafe
(eduflow.core.config_validation).
"""

from .base import *  # noqa: F403
from .base import env

# Read (not hard-coded) so a deployment that sets DJANGO_DEBUG=true fails loudly in validation below,
# instead of silently running with a setting its operator believes is on.
DEBUG = env.bool("DJANGO_DEBUG", default=False)
API_DOCS_ENABLED = env.bool("API_DOCS_ENABLED", default=False)

# TLS is terminated by the reverse proxy / load balancer in front of the app.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_SSL_REDIRECT = env.bool("DJANGO_SECURE_SSL_REDIRECT", default=True)
SECURE_REDIRECT_EXEMPT = [r"^api/v1/health/"]  # probes hit the pod directly over HTTP
SECURE_HSTS_SECONDS = env.int("DJANGO_SECURE_HSTS_SECONDS", default=31_536_000)
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = env.bool("DJANGO_SECURE_HSTS_PRELOAD", default=False)
# HSTS preload is effectively irreversible (browser preload lists), so it is an explicit operational
# decision per domain, not a default. Every other deploy check must pass.
SILENCED_SYSTEM_CHECKS = ["security.W021"]
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SESSION_COOKIE_HTTPONLY = True
CSRF_TRUSTED_ORIGINS = env.list("DJANGO_CSRF_TRUSTED_ORIGINS", default=[])

from eduflow.core.config_validation import validate_production_settings  # noqa: E402

validate_production_settings(globals())
