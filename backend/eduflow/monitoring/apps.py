from django.apps import AppConfig


class MonitoringConfig(AppConfig):
    """The monitoring intelligence layer: rules, alerts, risk, scorecards, pulse and Ask EduFlow."""

    name = "eduflow.monitoring"
    label = "monitoring"

    def ready(self) -> None:
        from eduflow.tenancy import jobs

        from . import engine, policies  # noqa: F401  (registers data-scope rules)

        jobs.register("frequent", "monitoring.evaluate", engine.evaluate)
