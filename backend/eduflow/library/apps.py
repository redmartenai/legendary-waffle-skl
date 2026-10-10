from django.apps import AppConfig


class LibraryConfig(AppConfig):
    """The library: catalogue, copies, loans, returns, renewals and fines."""

    name = "eduflow.library"
    label = "library"

    def ready(self) -> None:
        from eduflow.tenancy import jobs

        from . import policies, services  # noqa: F401  (registers data-scope rules)

        jobs.register("daily", "library.overdue_reminders", services.send_overdue_reminders)
