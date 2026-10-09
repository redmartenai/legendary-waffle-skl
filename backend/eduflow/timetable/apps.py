from django.apps import AppConfig


class TimetableConfig(AppConfig):
    """Timetables, periods, slots, lessons and schedules (Phase 5, ADR-026)."""

    name = "eduflow.timetable"
    label = "timetable"

    def ready(self) -> None:
        from . import policies  # noqa: F401  (registers data-scope rules)
