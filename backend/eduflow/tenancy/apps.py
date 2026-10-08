from django.apps import AppConfig


class TenancyConfig(AppConfig):
    """Schools (the tenant boundary), memberships and tenant resolution."""

    name = "eduflow.tenancy"
    label = "tenancy"
