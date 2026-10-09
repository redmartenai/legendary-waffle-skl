from django.apps import AppConfig


class BrandingConfig(AppConfig):
    """White-label branding: school colours, logo and favicon, and custom domains (ADR-029)."""

    name = "eduflow.branding"
    label = "branding"
