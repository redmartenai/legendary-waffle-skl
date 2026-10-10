from django.apps import AppConfig


class LmsConfig(AppConfig):
    """Learning: lessons, progress, learning paths, quizzes, live classes and learning analytics."""

    name = "eduflow.lms"
    label = "lms"

    def ready(self) -> None:
        from . import policies  # noqa: F401  (registers data-scope rules)
