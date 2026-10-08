from django.apps import AppConfig


class AuditConfig(AppConfig):
    """Append-only security and administration audit trail."""

    name = "eduflow.audit"
    label = "audit"
