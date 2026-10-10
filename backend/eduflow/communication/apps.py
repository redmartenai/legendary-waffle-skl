from django.apps import AppConfig


class CommunicationConfig(AppConfig):
    """Announcements, parent-teacher threads, complaints and feedback."""

    name = "eduflow.communication"
    label = "communication"

    def ready(self) -> None:
        from . import policies  # noqa: F401  (registers data-scope rules)
