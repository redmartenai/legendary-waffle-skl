from django.apps import AppConfig


class CoreConfig(AppConfig):
    """Cross-cutting infrastructure: request context, logging, errors, health. No domain logic."""

    name = "eduflow.core"
    label = "core"

    def ready(self) -> None:
        from . import celery_context  # noqa: F401  (connects Celery signal handlers)
