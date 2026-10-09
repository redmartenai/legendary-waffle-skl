from django.apps import AppConfig


class PeopleConfig(AppConfig):
    """Staff and teachers, students, guardians, enrollments and teacher assignments."""

    name = "eduflow.people"
    label = "people"

    def ready(self) -> None:
        from eduflow.academics.services import year_closing

        from . import policies  # noqa: F401  (registers data-scope rules, including those for sections)
        from .services import wind_down_year

        year_closing.connect(wind_down_year, dispatch_uid="people.wind_down_year")
