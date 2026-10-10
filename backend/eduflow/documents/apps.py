from django.apps import AppConfig


class DocumentsConfig(AppConfig):
    """The document repository and the stored-file record shared by every module."""

    name = "eduflow.documents"
    label = "documents"

    def ready(self) -> None:
        from . import policies  # noqa: F401  (registers data-scope rules)
