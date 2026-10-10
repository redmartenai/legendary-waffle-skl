from django.apps import AppConfig


class ReportsConfig(AppConfig):
    """Cross-module reports, as JSON or CSV exports (audited)."""

    name = "eduflow.reports"
    label = "reports"
