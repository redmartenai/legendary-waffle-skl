from django.apps import AppConfig


class FeesConfig(AppConfig):
    """Fee plans, student obligations, payments and receipts, refunds and defaulters."""

    name = "eduflow.fees"
    label = "fees"

    def ready(self) -> None:
        from . import approvals, policies  # noqa: F401  (registers scope rules and approval providers)

        approvals.register()
