from django.apps import AppConfig


class ConductConfig(AppConfig):
    """Teacher remarks (praise, concern, note) and behaviour incidents about students."""

    name = "eduflow.conduct"
    label = "conduct"

    def ready(self) -> None:
        from . import policies  # noqa: F401  (registers data-scope rules)
