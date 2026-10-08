"""Base API views. Every protected endpoint derives from one of these (docs/security/authorization.md).

=====================  ==================================================================================
View                   Checks, in order
=====================  ==================================================================================
AuthenticatedAPIView   valid access token on a live session -> no pending forced password change
TenantAPIView          ... -> active membership in the ``X-School-Id`` school -> the method's permission.
                       Data scope and object access are then applied by the view's ScopedResource.
PlatformAPIView        ... -> ``is_platform_admin``; the database context then lifts tenant restrictions.
=====================  ==================================================================================

``TenantAPIView.required_permissions`` maps each HTTP method to a permission codename. A method that is not
listed is refused, so a new handler is closed until someone states its permission.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from rest_framework.permissions import BasePermission, IsAuthenticated
from rest_framework.request import Request
from rest_framework.views import APIView

from eduflow.core import db_context
from eduflow.core.api import PasswordChangeRequired
from eduflow.core.logging import get_logger

from ..grants import Actor

log = get_logger(__name__)


class NoPendingPasswordChange(BasePermission):
    """A user created with a temporary password must replace it before doing anything else."""

    def has_permission(self, request: Request, view: APIView) -> bool:
        user = request.user
        if getattr(user, "must_change_password", False) and not getattr(
            view, "allow_pending_password", False
        ):
            raise PasswordChangeRequired()
        return True


class AuthenticatedAPIView(APIView):
    permission_classes = [IsAuthenticated, NoPendingPasswordChange]
    allow_pending_password = False


class TenantAPIView(AuthenticatedAPIView):
    required_permissions: Mapping[str, str] = {}
    actor: Actor

    def required_permission(self, method: str) -> str | None:
        return self.required_permissions.get("GET" if method == "HEAD" else method)

    def check_permissions(self, request: Request) -> None:
        super().check_permissions(request)
        from eduflow.tenancy.context import resolve_actor

        self.actor = resolve_actor(request)
        permission = self.required_permission(str(request.method))
        if permission is None:
            log.warning("authz_no_permission_declared", view=type(self).__name__, method=request.method)
            self.permission_denied(request)
        elif not self.actor.has(permission):
            log.info("authz_denied", permission=permission, view=type(self).__name__)
            self.permission_denied(request)


class PlatformAPIView(AuthenticatedAPIView):
    """Platform administration. Tenant restrictions are lifted only after the caller is verified."""

    def check_permissions(self, request: Request) -> None:
        super().check_permissions(request)
        if not getattr(request.user, "is_platform_admin", False):
            log.info("platform_denied", view=type(self).__name__)
            self.permission_denied(request)

    def initial(self, request: Request, *args: Any, **kwargs: Any) -> None:
        super().initial(request, *args, **kwargs)
        db_context.update(bypass=True)
        log.info("rls_bypass", reason="platform_api", view=type(self).__name__)
