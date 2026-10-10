from django.apps import AppConfig


class ApprovalsConfig(AppConfig):
    """The central approvals queue (aggregates module-owned requests; no tables of its own)."""

    name = "eduflow.approvals"
    label = "approvals"
