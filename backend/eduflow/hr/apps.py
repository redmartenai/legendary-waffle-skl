from django.apps import AppConfig


class HrConfig(AppConfig):
    """Staff attendance, leave, payroll and payslips, recruitment."""

    name = "eduflow.hr"
    label = "hr"

    def ready(self) -> None:
        from . import approvals, policies  # noqa: F401  (registers scope rules and approval providers)

        approvals.register()
