from typing import Any

from django.apps import AppConfig
from django.apps.registry import Apps
from django.db.models.signals import post_migrate


def _sync_permission_catalog(
    sender: AppConfig, using: str = "default", apps: Apps | None = None, **kwargs: Any
) -> None:
    if apps is not None:
        try:
            apps.get_model("authz", "Permission")
        except LookupError:  # authz has been migrated back to zero: there is no table to sync
            return
    from .services import sync_permission_catalog

    sync_permission_catalog(using=using)


class AuthzConfig(AppConfig):
    """Permissions, roles, data scopes and their enforcement."""

    name = "eduflow.authz"
    label = "authz"

    def ready(self) -> None:
        # Keep the permission table in step with catalog.PERMISSIONS after every migrate.
        post_migrate.connect(
            _sync_permission_catalog, sender=self, dispatch_uid="authz.sync_permission_catalog"
        )
