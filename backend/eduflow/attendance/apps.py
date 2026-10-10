from django.apps import AppConfig


class AttendanceConfig(AppConfig):
    """Daily section registers, attendance records and corrections (ADR-008, ADR-028)."""

    name = "eduflow.attendance"
    label = "attendance"

    def ready(self) -> None:
        from . import policies  # noqa: F401  (registers data-scope rules)
        from .approvals import register

        register()
