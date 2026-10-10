from django.apps import AppConfig


class TransportConfig(AppConfig):
    """School transport: vehicles, routes and stops, riders, trips, positions and maintenance."""

    name = "eduflow.transport"
    label = "transport"

    def ready(self) -> None:
        from . import policies  # noqa: F401  (registers data-scope rules)
