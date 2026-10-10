from django.apps import AppConfig


class AlumniConfig(AppConfig):
    """Alumni: the directory, events and registrations, campaigns and donations."""

    name = "eduflow.alumni"
    label = "alumni"

    def ready(self) -> None:
        from . import policies  # noqa: F401
