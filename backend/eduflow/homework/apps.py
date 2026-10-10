from django.apps import AppConfig


class HomeworkConfig(AppConfig):
    """Homework and assignments: set for a section and subject, submitted and reviewed."""

    name = "eduflow.homework"
    label = "homework"

    def ready(self) -> None:
        from . import policies  # noqa: F401  (registers data-scope rules)
