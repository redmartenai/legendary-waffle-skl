from django.apps import AppConfig


class AssessmentConfig(AppConfig):
    """Examinations, mark sheets, their approval and publication, corrections and report cards."""

    name = "eduflow.assessment"
    label = "assessment"

    def ready(self) -> None:
        from . import approvals, policies  # noqa: F401  (registers scope rules and approval providers)

        approvals.register()
