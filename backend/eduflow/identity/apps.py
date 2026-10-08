from django.apps import AppConfig


class IdentityConfig(AppConfig):
    """Users, authentication sessions, refresh tokens and OTP challenges."""

    name = "eduflow.identity"
    label = "identity"

    def ready(self) -> None:
        from . import schema  # noqa: F401  (registers the OpenAPI auth extension)
