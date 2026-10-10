from django.apps import AppConfig


class HostelConfig(AppConfig):
    """Hostels: rooms, bed allocation, outpasses and the nightly roll call."""

    name = "eduflow.hostel"
    label = "hostel"

    def ready(self) -> None:
        from . import approvals, policies  # noqa: F401  (registers scope rules and approval providers)

        approvals.register()
