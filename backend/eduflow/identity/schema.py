"""OpenAPI description of the bearer authentication (registered by import in IdentityConfig.ready)."""

from __future__ import annotations

from typing import Any

from drf_spectacular.extensions import OpenApiAuthenticationExtension


class AccessTokenScheme(OpenApiAuthenticationExtension):  # type: ignore[no-untyped-call]
    target_class = "eduflow.identity.authentication.AccessTokenAuthentication"
    name = "bearerAuth"

    def get_security_definition(self, auto_schema: Any) -> dict[str, Any]:
        return {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "JWT",
            "description": (
                "Access token from `/auth/password/login`, `/auth/otp/verify` or `/auth/token/refresh`. "
                "Valid for 10 minutes and only while its session is active "
                "(see docs/security/token-lifecycle.md)."
            ),
        }
