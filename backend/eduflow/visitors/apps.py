from django.apps import AppConfig


class VisitorsConfig(AppConfig):
    """Visitor management: pre-registration, security approval, QR passes and the gate log."""

    name = "eduflow.visitors"
    label = "visitors"

    def ready(self) -> None:
        from . import policies  # noqa: F401  (registers data-scope rules)
