from django.apps import AppConfig


class AdmissionsConfig(AppConfig):
    """Admissions: enquiry-to-enrolment pipeline, online applications, offer approvals."""

    name = "eduflow.admissions"
    label = "admissions"

    def ready(self) -> None:
        from .approvals import register

        register()
